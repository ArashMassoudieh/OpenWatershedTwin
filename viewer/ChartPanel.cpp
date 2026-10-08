#include "ChartPanel.h"

#include <QAreaSeries>
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
#include <QValueAxis>
#include <QVBoxLayout>
#include <cmath>
#include <limits>

namespace
{
const double kUnixSerial = 25569.0;   // 1970-01-01 as an OpenHydroQual day serial
qint64 toMs(double serial) { return qint64((serial - kUnixSerial) * 86400000.0); }

struct Range { double t0, t1; };

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

QChartView *makeChart(const QString &title, const QString &unit, const QJsonObject &s, const QJsonObject *s2,
                      const QString &label2, Range r, double now, bool bars, int height)
{
    auto *chart = new QChart;
    chart->setTitle(title);
    chart->legend()->setVisible(s2 != nullptr);
    chart->legend()->setAlignment(Qt::AlignBottom);
    chart->setMargins(QMargins(4, 4, 4, 4));
    auto *ax = new QDateTimeAxis;
    ax->setFormat((r.t1 - r.t0) > 120 ? "MMM yyyy" : "dd MMM");
    ax->setRange(QDateTime::fromMSecsSinceEpoch(toMs(r.t0), QTimeZone::UTC),
                 QDateTime::fromMSecsSinceEpoch(toMs(r.t1), QTimeZone::UTC));
    ax->setTickCount(6);
    auto *ay = new QValueAxis;
    ay->setTitleText(unit == "-" ? QString() : unit);
    chart->addAxis(ax, Qt::AlignBottom);
    chart->addAxis(ay, Qt::AlignLeft);

    double ymin = std::numeric_limits<double>::infinity(), ymax = -ymin;
    auto addLine = [&](const QJsonObject &o, const QString &name, const QColor &col, bool step) {
        auto *ls = new QLineSeries;
        ls->setName(name);
        const QJsonArray t = o.value("t").toArray(), v = o.value("v").toArray();
        double prevT = 0;
        for (int i = 0; i < t.size() && i < v.size(); ++i)
        {
            if (v[i].isNull()) continue;
            const double ti = t[i].toDouble(), vi = v[i].toDouble();
            if (ti < r.t0 - 1 || ti > r.t1 + 1) continue;
            if (step && i > 0) ls->append(toMs(prevT), vi);          // bars as a step line over each day
            ls->append(toMs(ti), vi);
            if (step) { ls->append(toMs(ti + 1.0), vi); }
            prevT = ti;
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
    addLine(s, s.value("label").toString(), bars ? QColor("#4292c6") : QColor("#2171b5"), bars);
    if (s2) addLine(*s2, label2, QColor("#d94801"), false);       // observations
    if (!std::isfinite(ymin)) { ymin = 0; ymax = 1; }
    if (bars) ymin = 0;
    const double pad = (ymax > ymin) ? 0.06 * (ymax - ymin) : 0.5;
    const double lowEdge = (bars || ymin >= 0) ? std::max(0.0, ymin - pad) : ymin - pad;   // non-negative stays >= 0
    ay->setRange(lowEdge, ymax + pad);

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
    auto *csv = new QPushButton(tr("CSV"));
    csv->setToolTip(tr("Save the series shown as CSV"));
    connect(csv, &QPushButton::clicked, this, &ChartPanel::exportCsv);
    head->addWidget(title_, 1);
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

void ChartPanel::show(const QString &title, const QJsonObject &doc, const QStringList &keys, TimeMode mode)
{
    clear(title);
    doc_ = doc;
    keys_ = keys;
    mode_ = mode;
    name_ = doc.value("id").toString();
    const QJsonObject series = doc.value("series").toObject();
    const double now = doc.value("now").toDouble();
    const Range r = timeWindow(doc, mode);
    int at = 0;
    if (series.contains("rain"))
    {
        const QJsonObject rain = series.value("rain").toObject();
        charts_->insertWidget(at++, makeChart(rain.value("label").toString(), rain.value("unit").toString(), rain,
                                              nullptr, QString(), r, now, true, 150));
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
                                              r, now, false, 230));
    }
}

void ChartPanel::exportCsv()
{
    if (doc_.isEmpty()) return;
    const QJsonObject series = doc_.value("series").toObject();
    const Range r = timeWindow(doc_, mode_);
    QByteArray out = "series,unit,time_utc,value\n";
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
            for (int i = 0; i < t.size() && i < v.size(); ++i)
            {
                const double ti = t[i].toDouble();
                if (ti < r.t0 || ti > r.t1 || v[i].isNull()) continue;
                out += (kk + "," + s.value("unit").toString() + "," +
                        QDateTime::fromMSecsSinceEpoch(toMs(ti), QTimeZone::UTC).toString(Qt::ISODate) + "," +
                        QString::number(v[i].toDouble(), 'g', 8) + "\n").toUtf8();
            }
        }
    }
    QFileDialog::saveFileContent(out, name_ + ".csv");
}
