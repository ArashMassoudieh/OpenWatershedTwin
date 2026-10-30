#include "ChartPanel.h"

#include <QAreaSeries>
#include <QCheckBox>
#include <QChart>
#include <QChartView>
#include <QDateTime>
#include <QDateTimeAxis>
#include <QFileDialog>
#include <QHBoxLayout>
#include <QJsonArray>
#include <QLabel>
#include <QLegendMarker>
#include <QTimeZone>
#include <QLineSeries>
#include <QPushButton>
#include <QScrollArea>
#include <QLogValueAxis>
#include <QValueAxis>
#include <QVBoxLayout>
#include <cmath>
#include <limits>

namespace
{
const double kUnixSerial = 25569.0;   // 1970-01-01 as an OpenHydroQual day serial
qint64 toMs(double serial) { return qint64((serial - kUnixSerial) * 86400000.0); }

struct Range { double t0, t1; };

// a unit file carries one time axis ("t") for all its series; give it to each series that has none
QJsonObject withTimes(const QJsonObject &doc)
{
    if (!doc.contains("t")) return doc;
    QJsonObject series = doc.value("series").toObject();
    for (auto it = series.begin(); it != series.end(); ++it)
    {
        QJsonObject s = it.value().toObject();
        if (s.contains("t")) continue;
        s["t"] = doc.value("t");
        it.value() = s;
    }
    QJsonObject d = doc;
    d["series"] = series;
    return d;
}

Range timeWindow(const QJsonObject &doc, TimeMode mode)
{
    const double now = doc.value("now").toDouble();
    double tmin = 1e18, tmax = -1e18;
    for (const QJsonValue &s : doc.value("series").toObject())
    {
        const QJsonArray t = s.toObject().value("t").toArray();
        if (t.isEmpty()) continue;
        tmin = std::min(tmin, t.first().toDouble());
        tmax = std::max(tmax, t.last().toDouble());
    }
    if (mode == TimeMode::History) return {tmin, now};
    if (mode == TimeMode::Forecast) return {now - 3.0, tmax};
    return {std::max(tmin, now - 45.0), tmax};       // recent history and the forecast
}

bool hasParamBand(const QJsonObject &s) { return s.contains("lo") && s.contains("hi"); }
// "pred_sd": log-space standard deviation of a multiplicative, log-normal error of the series (model structure,
// forcing and measurement error, from the calibration; viewer_config "prediction_sd_log"), set by MainWindow
bool hasPrediction(const QJsonObject &s) { return s.value("pred_sd").toDouble() > 0; }
bool hasBand(const QJsonObject &s) { return hasParamBand(s) || hasPrediction(s); }
constexpr double kZ95 = 1.959964;

// 95 % prediction interval of value v: the log-normal error combined with the parameter band (treated as log-normal,
// its 2.5..97.5 % range = +-kZ95 sd), i.e. v exp(+-kZ95 sqrt(sd^2 + sd_par^2)); false where v <= 0
bool predictionAt(const QJsonObject &s, int i, double &lo, double &hi)
{
    const QJsonArray v = s.value("v").toArray();
    if (i >= v.size() || v[i].isNull()) return false;
    const double vi = v[i].toDouble();
    if (!(vi > 0)) return false;
    const double sd = s.value("pred_sd").toDouble();
    double sp = 0;
    if (hasParamBand(s))
    {
        const QJsonArray l = s.value("lo").toArray(), h = s.value("hi").toArray();
        if (i < l.size() && i < h.size() && !l[i].isNull() && !h[i].isNull() && l[i].toDouble() > 0 && h[i].toDouble() > 0)
            sp = std::log(h[i].toDouble() / l[i].toDouble()) / (2 * kZ95);
    }
    const double w = kZ95 * std::sqrt(sd * sd + sp * sp);
    lo = vi * std::exp(-w);
    hi = vi * std::exp(w);
    return true;
}

QChartView *makeChart(const QString &title, const QString &unit, const QJsonObject &s, const QJsonObject *s2,
                      const QString &label2, Range r, double now, bool bars, int height, bool band)
{
    auto *chart = new QChart;
    chart->setTitle(title);
    chart->legend()->setVisible(s2 != nullptr);
    chart->legend()->setAlignment(Qt::AlignBottom);
    chart->setMargins(QMargins(4, 4, 4, 4));
    auto *ax = new QDateTimeAxis;
    ax->setFormat((r.t1 - r.t0) > 120 ? "MMM yyyy" : (r.t1 - r.t0) > 12 ? "dd MMM" : "dd MMM hh:mm");
    ax->setRange(QDateTime::fromMSecsSinceEpoch(toMs(r.t0), QTimeZone::UTC),
                 QDateTime::fromMSecsSinceEpoch(toMs(r.t1), QTimeZone::UTC));
    ax->setTickCount(6);
    // "chart_log" (viewer_config, set by MainWindow): logarithmic value axis; non-positive points are left out
    const bool logy = !bars && s.value("chart_log").toBool();
    QAbstractAxis *ay;
    if (logy)
    {
        auto *la = new QLogValueAxis;
        la->setBase(10);
        la->setLabelFormat("%g");
        la->setMinorTickCount(8);
        ay = la;
    }
    else
        ay = new QValueAxis;
    ay->setTitleText(unit == "-" ? QString() : unit);
    chart->addAxis(ax, Qt::AlignBottom);
    chart->addAxis(ay, Qt::AlignLeft);

    double ymin = std::numeric_limits<double>::infinity(), ymax = -ymin;
    auto addLine = [&](const QJsonObject &o, const QString &name, const QColor &col, bool step) {
        auto *ls = new QLineSeries;
        ls->setName(name);
        const QJsonArray t = o.value("t").toArray(), v = o.value("v").toArray();
        double width = 1.0;                                          // bar width = the series' time step
        for (int i = 0; i < t.size() && i < v.size(); ++i)
        {
            if (i + 1 < t.size()) width = t[i + 1].toDouble() - t[i].toDouble();
            if (v[i].isNull()) continue;
            const double ti = t[i].toDouble(), vi = v[i].toDouble();
            if (logy && !(vi > 0)) continue;
            if (ti < r.t0 - 1 || ti > r.t1 + 1) continue;
            ls->append(toMs(ti), vi);                                // bars as a step line over each interval
            if (step) ls->append(toMs(ti + width), vi);
            ymin = std::min(ymin, vi);
            ymax = std::max(ymax, vi);
        }
        QPen pen(col);
        pen.setWidthF(step ? 1.0 : 1.6);
        ls->setPen(pen);
        if (step)
        {
            auto *base = new QLineSeries;
            for (const QPointF &pt : ls->points()) base->append(pt.x(), 0);
            auto *area = new QAreaSeries(ls, base);
            area->setName(name);
            area->setBrush(QColor(66, 146, 198, 150));
            area->setPen(pen);
            chart->addSeries(area);
            area->attachAxis(ax);
            area->attachAxis(ay);
        }
        else
        {
            chart->addSeries(ls);
            ls->attachAxis(ax);
            ls->attachAxis(ay);
        }
    };
    // prediction band (lightest), behind the parameter band
    if (band && !bars && hasPrediction(s))
    {
        auto *up = new QLineSeries, *dn = new QLineSeries;
        const QJsonArray t = s.value("t").toArray();
        for (int i = 0; i < t.size(); ++i)
        {
            const double ti = t[i].toDouble();
            if (ti < r.t0 - 1 || ti > r.t1 + 1) continue;
            double plo, phi;
            if (!predictionAt(s, i, plo, phi)) continue;
            up->append(toMs(ti), phi);
            dn->append(toMs(ti), plo);
            ymin = std::min(ymin, plo);
            ymax = std::max(ymax, phi);
        }
        if (up->count() > 1)
        {
            auto *area = new QAreaSeries(up, dn);
            area->setName(QObject::tr("95% prediction"));
            area->setBrush(QColor(107, 174, 214, 90));
            area->setPen(Qt::NoPen);
            chart->addSeries(area);
            area->attachAxis(ax);
            area->attachAxis(ay);
            chart->legend()->setVisible(true);
        }
        else { delete up; delete dn; }
    }
    // parameter band (lo..hi) behind the model line
    if (band && !bars && hasParamBand(s))
    {
        auto *up = new QLineSeries, *dn = new QLineSeries;
        const QJsonArray t = s.value("t").toArray(), lo = s.value("lo").toArray(), hi = s.value("hi").toArray();
        for (int i = 0; i < t.size() && i < lo.size() && i < hi.size(); ++i)
        {
            if (lo[i].isNull() || hi[i].isNull()) continue;
            if (logy && !(lo[i].toDouble() > 0)) continue;
            const double ti = t[i].toDouble();
            if (ti < r.t0 - 1 || ti > r.t1 + 1) continue;
            up->append(toMs(ti), hi[i].toDouble());
            dn->append(toMs(ti), lo[i].toDouble());
            ymin = std::min(ymin, lo[i].toDouble());
            ymax = std::max(ymax, hi[i].toDouble());
        }
        if (up->count() > 1)
        {
            auto *area = new QAreaSeries(up, dn);
            area->setName(hasPrediction(s) ? QObject::tr("95% parameters") : QObject::tr("95% interval"));
            area->setBrush(QColor(33, 113, 181, hasPrediction(s) ? 150 : 90));
            area->setPen(Qt::NoPen);
            chart->addSeries(area);
            area->attachAxis(ax);
            area->attachAxis(ay);
            chart->legend()->setVisible(true);
        }
        else { delete up; delete dn; }
    }
    addLine(s, s.value("label").toString(), bars ? QColor("#4292c6") : QColor("#2171b5"), bars);
    if (s2) addLine(*s2, label2, QColor("#d94801"), false);       // observations
    if (!std::isfinite(ymin)) { ymin = 0; ymax = 1; }
    if (bars) ymin = 0;
    double pad = (ymax > ymin) ? 0.06 * (ymax - ymin) : 0.5;
    double lowEdge = (bars || ymin >= 0) ? std::max(0.0, ymin - pad) : ymin - pad;   // non-negative stays >= 0
    if (logy)
    {
        if (!(ymin > 0)) { ymin = 1e-3; ymax = std::max(ymax, 1.0); }
        lowEdge = ymin / 1.5;                                        // multiplicative padding on a log axis
        pad = ymax * 0.5;
        static_cast<QLogValueAxis *>(ay)->setRange(lowEdge, ymax + pad);
    }
    else
        static_cast<QValueAxis *>(ay)->setRange(lowEdge, ymax + pad);

    // forecast shading and the "now" line
    if (now < r.t1)
    {
        auto *up = new QLineSeries, *lo = new QLineSeries;
        up->append(toMs(std::max(now, r.t0)), ymax + pad); up->append(toMs(r.t1), ymax + pad);
        lo->append(toMs(std::max(now, r.t0)), lowEdge); lo->append(toMs(r.t1), lowEdge);
        auto *shade = new QAreaSeries(up, lo);
        shade->setBrush(QColor(253, 174, 107, 50));
        shade->setPen(Qt::NoPen);
        chart->addSeries(shade);
        shade->attachAxis(ax);
        shade->attachAxis(ay);
        auto *nowLine = new QLineSeries;
        nowLine->append(toMs(now), lowEdge);
        nowLine->append(toMs(now), ymax + pad);
        nowLine->setPen(QPen(QColor("#d94801"), 1.2, Qt::DashLine));
        chart->addSeries(nowLine);
        nowLine->attachAxis(ax);
        nowLine->attachAxis(ay);
        for (QLegendMarker *m : chart->legend()->markers(shade)) m->setVisible(false);
        for (QLegendMarker *m : chart->legend()->markers(nowLine)) m->setVisible(false);
    }
    auto *view = new QChartView(chart);
    view->setRenderHint(QPainter::Antialiasing);
    view->setMinimumHeight(height);
    return view;
}
} // namespace

ChartPanel::ChartPanel(QWidget *parent) : QWidget(parent)
{
    auto *outer = new QVBoxLayout(this);
    outer->setContentsMargins(4, 4, 4, 4);
    auto *head = new QHBoxLayout;
    title_ = new QLabel(tr("Right-click a sub-catchment or a gage on the map."));
    title_->setWordWrap(true);
    QFont f = title_->font();
    f.setBold(true);
    title_->setFont(f);
    intervals_ = new QCheckBox(tr("95% intervals"));
    intervals_->setChecked(false);
    intervals_->setEnabled(false);
    intervals_->setToolTip(tr("No uncertainty band for the series shown"));
    connect(intervals_, &QCheckBox::toggled, this, [this]() {
        if (!doc_.isEmpty()) show(title_->text(), doc_, keys_, mode_);
    });
    auto *csv = new QPushButton(tr("CSV"));
    csv->setToolTip(tr("Save the series shown as CSV"));
    connect(csv, &QPushButton::clicked, this, &ChartPanel::exportCsv);
    head->addWidget(title_, 1);
    head->addWidget(intervals_);
    head->addWidget(csv);
    outer->addLayout(head);
    auto *scroll = new QScrollArea;
    scroll->setWidgetResizable(true);
    auto *inner = new QWidget;
    charts_ = new QVBoxLayout(inner);
    charts_->setContentsMargins(0, 0, 0, 0);
    charts_->addStretch(1);
    scroll->setWidget(inner);
    outer->addWidget(scroll, 1);
}

void ChartPanel::clear(const QString &message)
{
    while (charts_->count() > 1)
    {
        QLayoutItem *it = charts_->takeAt(0);
        delete it->widget();
        delete it;
    }
    title_->setText(message);
    doc_ = QJsonObject();
    keys_.clear();
}

void ChartPanel::setIntervalText(const QString &label, const QString &tooltip)
{
    if (!label.isEmpty()) intervals_->setText(label);
    intervalTip_ = tooltip;
}

void ChartPanel::show(const QString &title, const QJsonObject &unitDoc, const QStringList &keysIn, TimeMode mode)
{
    // copy before clear(): the "95% intervals" checkbox re-shows with doc_ and keys_ themselves, which clear() empties
    const QJsonObject doc = withTimes(unitDoc);
    const QStringList keys = keysIn;
    clear(title);
    doc_ = doc;
    keys_ = keys;
    mode_ = mode;
    name_ = doc.value("id").toString();
    const QJsonObject series = doc.value("series").toObject();
    const double now = doc.value("now").toDouble();
    const Range r = timeWindow(doc, mode);
    bool anyBand = false;
    for (const QString &k : keys) anyBand = anyBand || hasBand(series.value(k).toObject());
    intervals_->setEnabled(anyBand);
    intervals_->setToolTip(anyBand ? (intervalTip_.isEmpty() ? tr("Shade the 95% interval of the model") : intervalTip_)
                                   : tr("No uncertainty band for the series shown"));
    const bool band = anyBand && intervals_->isChecked();
    int at = 0;
    if (series.contains("rain"))
    {
        const QJsonObject rain = series.value("rain").toObject();
        charts_->insertWidget(at++, makeChart(rain.value("label").toString(), rain.value("unit").toString(), rain,
                                              nullptr, QString(), r, now, true, 150, false));
    }
    for (const QString &k : keys)
    {
        if (!series.contains(k)) continue;
        const QJsonObject s = series.value(k).toObject();
        // gages: show the observation with the model ("Q_model" + "Q_obs")
        QJsonObject obs;
        const bool paired = k.endsWith("_model") && series.contains(k.left(k.size() - 6) + "_obs");
        if (paired) obs = series.value(k.left(k.size() - 6) + "_obs").toObject();
        charts_->insertWidget(at++, makeChart(s.value("label").toString(), s.value("unit").toString(), s,
                                              paired ? &obs : nullptr, paired ? obs.value("label").toString() : QString(),
                                              r, now, false, 230, band));
    }
}

void ChartPanel::exportCsv()
{
    if (doc_.isEmpty()) return;
    const QJsonObject series = doc_.value("series").toObject();
    const Range r = timeWindow(doc_, mode_);
    const bool band = intervals_->isEnabled() && intervals_->isChecked();
    QByteArray out = band ? "series,unit,time_utc,value,lo,hi,pred_lo,pred_hi\n" : "series,unit,time_utc,value\n";
    QStringList keys = keys_;
    if (series.contains("rain")) keys.prepend("rain");
    for (const QString &k : keys)
    {
        QStringList all{k};
        if (k.endsWith("_model")) all << k.left(k.size() - 6) + "_obs";
        for (const QString &kk : all)
        {
            const QJsonObject s = series.value(kk).toObject();
            const QJsonArray t = s.value("t").toArray(), v = s.value("v").toArray();
            const QJsonArray lo = s.value("lo").toArray(), hi = s.value("hi").toArray();
            auto num = [](const QJsonArray &a, int i) {
                return i < a.size() && !a[i].isNull() ? QString::number(a[i].toDouble(), 'g', 8) : QString();
            };
            for (int i = 0; i < t.size() && i < v.size(); ++i)
            {
                const double ti = t[i].toDouble();
                if (ti < r.t0 || ti > r.t1 || v[i].isNull()) continue;
                QString pred = ",,";
                double plo, phi;
                if (hasPrediction(s) && predictionAt(s, i, plo, phi))
                    pred = "," + QString::number(plo, 'g', 8) + "," + QString::number(phi, 'g', 8);
                out += (kk + "," + s.value("unit").toString() + "," +
                        QDateTime::fromMSecsSinceEpoch(toMs(ti), QTimeZone::UTC).toString(Qt::ISODate) + "," +
                        QString::number(v[i].toDouble(), 'g', 8) +
                        (band ? "," + num(lo, i) + "," + num(hi, i) + pred : QString()) + "\n").toUtf8();
            }
        }
    }
    QFileDialog::saveFileContent(out, name_ + ".csv");
}
