#include "FloodAlertDialog.h"

#include "DataSource.h"

#include <QAreaSeries>
#include <QCategoryAxis>
#include <QChart>
#include <QChartView>
#include <QDesktopServices>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QJsonArray>
#include <QLabel>
#include <QLineSeries>
#include <QLocale>
#include <QPainter>
#include <QPointer>
#include <QProgressBar>
#include <QPushButton>
#include <QSplitter>
#include <QTabWidget>
#include <QTableWidget>
#include <QTimeZone>
#include <QValueAxis>
#include <QVBoxLayout>
#include <algorithm>
#include <cmath>
#include <limits>

// ------------------------------------------------------------------------------------------------ formatting
namespace alertfmt
{
namespace
{
const QLocale &us()
{
    static const QLocale l(QLocale::English, QLocale::UnitedStates);
    return l;
}

// n-th Sunday (n >= 1) of a month, as a date
QDate nthSunday(int year, int month, int n)
{
    QDate d(year, month, 1);
    while (d.dayOfWeek() != Qt::Sunday) d = d.addDays(1);
    return d.addDays(7 * (n - 1));
}

// US Eastern time by the rules in force since 2007 (EDT from the second Sunday of March 2:00 EST to the first Sunday
// of November 2:00 EDT). Computed here because the WebAssembly build has no time-zone database.
bool easternDst(const QDateTime &utc)
{
    const int y = utc.date().year();
    const QDateTime start(nthSunday(y, 3, 2), QTime(7, 0), QTimeZone::UTC);   // 2:00 EST = 07:00 UTC
    const QDateTime end(nthSunday(y, 11, 1), QTime(6, 0), QTimeZone::UTC);    // 2:00 EDT = 06:00 UTC
    return utc >= start && utc < end;
}

} // namespace

QDateTime easternWall(const QDateTime &utc, QString *abbrev)
{
    const bool dst = easternDst(utc);
    if (abbrev) *abbrev = dst ? "EDT" : "EST";
    const QDateTime shifted = utc.toUTC().addSecs(dst ? -4 * 3600 : -5 * 3600);
    return QDateTime(shifted.date(), shifted.time());           // wall-clock fields only
}

QDateTime parseUtc(const QString &iso)
{
    QDateTime t = QDateTime::fromString(iso, Qt::ISODate);
    if (!t.isValid()) t = QDateTime::fromString(iso, Qt::ISODateWithMs);
    if (t.isValid()) t = t.toUTC();
    return t;
}

QString localTime(const QDateTime &utc, bool withDate)
{
    if (!utc.isValid()) return QStringLiteral("?");
    QString tz;
    const QDateTime e = easternWall(utc, &tz);
    return us().toString(e, withDate ? "ddd d MMM yyyy, h:mm AP" : "h:mm AP") + " " + tz;
}

QString cfs(double cms, bool round3)
{
    if (!std::isfinite(cms)) return QStringLiteral("?");
    const double q = cms * kCfsPerCms;
    if (!round3) return us().toString(qint64(std::round(q))) + " cfs";
    // three significant figures, no false precision
    const double mag = std::pow(10.0, std::floor(std::log10(std::max(q, 1.0))) - 2);
    const double r = q >= 100 ? std::round(q / mag) * mag : std::round(q);
    return us().toString(qint64(r)) + " cfs";
}

QString duration(qint64 s)
{
    const bool neg = s < 0;
    s = std::llabs(s);
    const qint64 h = s / 3600, m = (s % 3600 + 30) / 60;
    QString t = h > 0 ? QString("%1 h %2 min").arg(h).arg(m, 2, 10, QChar('0')) : QString("%1 min").arg(m);
    return neg ? "-" + t : t;
}

QString stateText(const QString &state)
{
    if (state == "watch") return QObject::tr("Watch");
    if (state == "triggered") return QObject::tr("Triggered");
    if (state == "mapping") return QObject::tr("Mapping");
    if (state == "mapped") return QObject::tr("Mapped");
    if (state == "none" || state.isEmpty()) return QObject::tr("No alert");
    return state;
}

QString memberLabel(const QString &member)
{
    bool ok = false;
    const int p = member.mid(1).toInt(&ok);
    if (!member.startsWith('p') || !ok) return member;
    const int last = p % 10, lastTwo = p % 100;
    const QString suf = (lastTwo >= 11 && lastTwo <= 13) ? "th" : last == 1 ? "st" : last == 2 ? "nd" : last == 3 ? "rd" : "th";
    return QObject::tr("%1%2 percentile member").arg(p).arg(suf);
}

QColor stateColor(const QString &state)
{
    if (state == "watch") return QColor("#d69e2e");                                   // amber
    if (state == "triggered" || state == "mapping" || state == "mapped") return QColor("#c53030");   // red
    return QColor("#718096");
}
} // namespace alertfmt

using namespace alertfmt;

// ------------------------------------------------------------------------------------------------ FitImage
FitImage::FitImage(QWidget *parent) : QWidget(parent)
{
    setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Expanding);
    setMinimumSize(200, 200);
}

void FitImage::paintEvent(QPaintEvent *)
{
    QPainter p(this);
    p.fillRect(rect(), QColor("#f4f4f2"));
    if (pix_.isNull())
    {
        p.setPen(QColor("#555555"));
        p.drawText(rect(), Qt::AlignCenter | Qt::TextWordWrap, msg_.isEmpty() ? tr("Loading map...") : msg_);
        return;
    }
    const QSize s = pix_.size().scaled(size(), Qt::KeepAspectRatio);
    const QRect r(QPoint((width() - s.width()) / 2, (height() - s.height()) / 2), s);
    p.setRenderHint(QPainter::SmoothPixmapTransform);
    p.drawPixmap(r, pix_);
}

// ------------------------------------------------------------------------------------------------ dialog
FloodAlertDialog::FloodAlertDialog(DataSource *data, const QString &outputs, QWidget *parent)
    : QDialog(parent), data_(data), outputs_(outputs)
{
    setModal(false);
    setSizeGripEnabled(true);
    setWindowTitle(tr("Flood alert"));
    auto *v = new QVBoxLayout(this);
    header_ = new QLabel;
    header_->setTextFormat(Qt::RichText);
    header_->setWordWrap(true);
    header_->setTextInteractionFlags(Qt::TextSelectableByMouse);
    v->addWidget(header_);
    progress_ = new QProgressBar;
    progress_->setRange(0, 100);
    progress_->setMaximumHeight(16);
    progress_->hide();
    v->addWidget(progress_);

    auto *split = new QSplitter(Qt::Vertical);
    tabs_ = new QTabWidget;
    split->addWidget(tabs_);
    chartBox_ = new QWidget;
    auto *cb = new QVBoxLayout(chartBox_);
    cb->setContentsMargins(0, 0, 0, 0);
    split->addWidget(chartBox_);
    split->setStretchFactor(0, 3);
    split->setStretchFactor(1, 1);
    v->addWidget(split, 1);

    links_ = new QHBoxLayout;
    v->addLayout(links_);
    resize(1100, 900);
}

QString FloodAlertDialog::resolve(const QString &rel) const
{
    if (rel.startsWith("http://") || rel.startsWith("https://") || rel.startsWith('/')) return rel;
    return outputs_ + rel;
}

void FloodAlertDialog::setAlert(const QJsonObject &alert)
{
    if (alert == alert_ && tabs_->count() > 0) return;
    alert_ = alert;
    rebuild();
}

void FloodAlertDialog::selectMember(const QString &member)
{
    for (int i = 0; i < tabs_->count(); ++i)
        if (tabs_->widget(i)->property("member").toString() == member) tabs_->setCurrentIndex(i);
}

namespace
{
QString statisticText(const QString &st)
{
    if (st == "p50") return QObject::tr("ensemble median (p50) of the forecast peak flow");
    if (st.startsWith('p')) return QObject::tr("%1 percentile (%2) of the forecast peak flow").arg(st.mid(1) + "th", st);
    return st;
}

QString prettyKey(QString k)
{
    for (const char *suf : {"_ft", "_m", "_mi", "_acres", "_ha"})
        if (k.endsWith(suf)) k.chop(int(strlen(suf)));
    k.replace('_', ' ');
    if (!k.isEmpty()) k[0] = k[0].toUpper();
    return k;
}

QString lengthFt(double ft)
{
    QString s = QLocale(QLocale::English, QLocale::UnitedStates).toString(qint64(std::round(ft))) + " ft";
    if (ft >= 1320) s += QString(" (%1 mi)").arg(ft / 5280.0, 0, 'f', 2);
    return s;
}

QString itemText(const QJsonValue &v)
{
    if (v.isString()) return v.toString();
    if (v.isDouble()) return QString::number(v.toDouble(), 'g', 4);
    const QJsonObject o = v.toObject();
    QString s = o.value("name").toString(o.value("label").toString());
    if (o.contains("length_ft")) s += ": " + lengthFt(o.value("length_ft").toDouble());
    else if (o.contains("length_m")) s += ": " + lengthFt(o.value("length_m").toDouble() / 0.3048);
    if (o.contains("max_depth_ft")) s += QString(", up to %1 ft deep").arg(o.value("max_depth_ft").toDouble(), 0, 'f', 1);
    return s;
}

// rows (feature, value) of an impacts object: lengths in ft (and miles), lists, counts
QList<QPair<QString, QString>> impactRows(const QJsonObject &imp)
{
    QList<QPair<QString, QString>> lengths, lists, counts;
    for (auto it = imp.constBegin(); it != imp.constEnd(); ++it)
    {
        const QString k = it.key();
        const QJsonValue v = it.value();
        if (v.isDouble() && k.endsWith("_ft")) lengths << qMakePair(prettyKey(k), lengthFt(v.toDouble()));
        else if (v.isDouble() && k.endsWith("_m")) lengths << qMakePair(prettyKey(k), lengthFt(v.toDouble() / 0.3048));
        else if (v.isDouble() && k.endsWith("_mi")) lengths << qMakePair(prettyKey(k), lengthFt(v.toDouble() * 5280.0));
        else if (v.isDouble() && k.endsWith("_acres")) counts << qMakePair(prettyKey(k), QString("%1 acres").arg(v.toDouble(), 0, 'f', 1));
        else if (v.isDouble() && k.endsWith("_ha")) counts << qMakePair(prettyKey(k), QString("%1 acres").arg(v.toDouble() * 2.47105, 0, 'f', 1));
        else if (v.isArray())
        {
            // one row per item (cross streets), the feature name on the first
            const QJsonArray a = v.toArray();
            const QString name = QString("%1 (%2)").arg(prettyKey(k)).arg(a.size());
            if (a.isEmpty()) lists << qMakePair(name, QObject::tr("none"));
            for (int i = 0; i < a.size(); ++i) lists << qMakePair(i == 0 ? name : QString(), itemText(a[i]));
        }
        else if (v.isObject())
        {
            const QJsonObject o = v.toObject();   // one copy: begin/end of two temporaries would not match
            for (auto jt = o.constBegin(); jt != o.constEnd(); ++jt)
                lists << qMakePair(prettyKey(k) + ": " + jt.key(), itemText(jt.value()));
        }
        else if (v.isDouble())
        {
            const double x = v.toDouble();
            counts << qMakePair(prettyKey(k), x == std::round(x) ? QString::number(qint64(x)) : QString::number(x, 'g', 4));
        }
        else if (v.isString())
            counts << qMakePair(prettyKey(k), v.toString());
    }
    // the parkway and trail first
    std::stable_sort(lengths.begin(), lengths.end(), [](const auto &a, const auto &b) {
        auto rank = [](const QString &s) { return s.contains("Parkway") ? 0 : s.contains("Trail") ? 1 : 2; };
        return rank(a.first) < rank(b.first);
    });
    return lengths + lists + counts;
}
} // namespace

void FloodAlertDialog::rebuild()
{
    const QString state = alert_.value("state").toString();
    const QString stat = alert_.value("statistic").toString("p50");
    const QJsonObject fc = alert_.value("forecast").toObject();
    const QJsonObject peaks = fc.value("peak_cms").toObject(), ptimes = fc.value("peak_time_utc").toObject();
    const QDateTime issued = parseUtc(alert_.value("issued_utc").toString());
    const double thr = alert_.value("threshold_cms").toDouble(std::numeric_limits<double>::quiet_NaN());
    const QString label = alert_.value("label").toString(alert_.value("site").toString());
    setWindowTitle(tr("Flood alert: %1").arg(alert_.value("viewer").toObject().value("label").toString(label)));

    // header
    QString h = QString("<div style='font-size:15pt; font-weight:bold'>%1</div>").arg(label.toHtmlEscaped());
    h += QString("<p><span style='background:%1; color:white; font-weight:bold'>&nbsp;%2&nbsp;</span>"
                 "&nbsp;&nbsp;Issued %3</p>")
             .arg(stateColor(state).name(), stateText(state).toUpper(), localTime(issued));
    QString trig = tr("Trigger: %1").arg(statisticText(stat));
    if (peaks.contains(stat)) trig += tr(" = <b>%1</b>").arg(cfs(peaks.value(stat).toDouble()));
    if (std::isfinite(thr)) trig += tr(", threshold <b>%1</b>").arg(cfs(thr, false));
    if (alert_.contains("trigger_label")) trig += " (" + alert_.value("trigger_label").toString().toHtmlEscaped() + ")";
    h += "<p style='margin:2px'>" + trig + "</p>";
    QStringList pk;
    for (const QString &m : peaks.keys())
    {
        const QDateTime tp = parseUtc(ptimes.value(m).toString());
        QString s = QString("%1: <b>%2</b>").arg(m, cfs(peaks.value(m).toDouble()));
        if (tp.isValid())
        {
            s += tr(" at %1").arg(localTime(tp, false));
            if (issued.isValid()) s += tr(" (lead %1)").arg(duration(issued.secsTo(tp)));
        }
        pk << s;
    }
    if (!pk.isEmpty()) h += "<p style='margin:2px'>" + tr("Forecast peak at the trigger reach: ") + pk.join("&nbsp;&nbsp;|&nbsp;&nbsp;") + "</p>";
    const QJsonObject job = alert_.value("job").toObject();
    if (!job.isEmpty())
    {
        const QString js = job.value("state").toString();
        QString jt = tr("2D flood mapping job %1: %2").arg(job.value("id").toString().toHtmlEscaped(), js.toHtmlEscaped());
        if (job.contains("message")) jt += " (" + job.value("message").toString().toHtmlEscaped() + ")";
        h += "<p style='margin:2px; color:#444'>" + jt + "</p>";
        const double pr = job.value("progress").toDouble(-1);
        progress_->setVisible(pr >= 0 && js != "done");
        progress_->setValue(int(std::round(100 * std::clamp(pr, 0.0, 1.0))));
    }
    else
        progress_->hide();
    header_->setText(h);

    // member tabs (keep the selected one)
    const QString current = tabs_->currentWidget() ? tabs_->currentWidget()->property("member").toString() : QString();
    while (tabs_->count() > 0) { QWidget *w = tabs_->widget(0); tabs_->removeTab(0); w->deleteLater(); }
    const QJsonObject maps = alert_.value("maps").toObject();
    QStringList members = maps.keys();
    std::sort(members.begin(), members.end(), [](const QString &a, const QString &b) { return a.mid(1).toInt() < b.mid(1).toInt(); });
    for (const QString &m : members)
    {
        QWidget *page = memberPage(m, maps.value(m).toObject());
        tabs_->addTab(page, memberLabel(m));
    }
    if (members.isEmpty())
    {
        auto *l = new QLabel(state == "watch" ? tr("Watch: the forecast is approaching the threshold; no flood maps yet.")
                                              : tr("No flood maps yet."));
        l->setAlignment(Qt::AlignCenter);
        tabs_->addTab(l, tr("Maps"));
    }
    if (!current.isEmpty()) selectMember(current);

    // forecast band
    QLayout *cl = chartBox_->layout();
    while (QLayoutItem *it = cl->takeAt(0)) { if (it->widget()) it->widget()->deleteLater(); delete it; }
    if (QChartView *cv = bandChart()) { cl->addWidget(cv); chartBox_->show(); }
    else chartBox_->hide();

    // links: demo page, animations (top-level "animations" and per-member "animation")
    while (QLayoutItem *it = links_->takeAt(0)) { if (it->widget()) it->widget()->deleteLater(); delete it; }
    auto addLink = [this](const QString &text, const QUrl &url) {
        auto *b = new QPushButton(text);
        b->setToolTip(url.toString());
        connect(b, &QPushButton::clicked, this, [url]() { QDesktopServices::openUrl(url); });
        links_->addWidget(b);
    };
    if (alert_.contains("demo_url")) addLink(tr("Open demo page"), QUrl(alert_.value("demo_url").toString()));
    for (const QJsonValue &av : alert_.value("animations").toArray())
    {
        const QJsonObject a = av.toObject();
        addLink(a.value("label").toString(tr("Animation")), data_->urlFor(resolve(a.value("url").toString())));
    }
    for (const QString &m : members)
    {
        const QString anim = maps.value(m).toObject().value("animation").toString();
        if (!anim.isEmpty()) addLink(tr("Animation, %1").arg(memberLabel(m)), data_->urlFor(resolve(anim)));
    }
    links_->addStretch(1);
    auto *close = new QPushButton(tr("Close"));
    connect(close, &QPushButton::clicked, this, &QDialog::close);
    links_->addWidget(close);
}

QWidget *FloodAlertDialog::memberPage(const QString &member, const QJsonObject &map)
{
    auto *page = new QSplitter(Qt::Horizontal);
    page->setProperty("member", member);
    auto *img = new FitImage;
    page->addWidget(img);

    auto *side = new QWidget;
    auto *sv = new QVBoxLayout(side);
    sv->setContentsMargins(6, 0, 0, 0);
    const QString src = map.value("source").toString();
    const QString state = alert_.value("state").toString();
    QString srcText = src == "2d" ? tr("2D simulation") : src == "library" ? tr("pre-built library map") : src;
    if (src == "library" && (state == "mapping" || state == "triggered")) srcText += tr(" (2D simulation running)");
    QString info = QString("<b>%1</b><br>").arg(memberLabel(member));
    info += tr("Map: %1").arg(srcText) + "<br>";
    if (map.contains("stage_ft")) info += tr("Gauge stage at the peak: <b>%1 ft</b>").arg(map.value("stage_ft").toDouble(), 0, 'f', 2) + "<br>";
    const QJsonObject fc = alert_.value("forecast").toObject();
    const double pk = fc.value("peak_cms").toObject().value(member).toDouble(std::numeric_limits<double>::quiet_NaN());
    if (std::isfinite(pk))
    {
        info += tr("Forecast peak: <b>%1</b>").arg(cfs(pk));
        const QDateTime tp = parseUtc(fc.value("peak_time_utc").toObject().value(member).toString());
        if (tp.isValid()) info += tr(" at %1").arg(localTime(tp));
        info += "<br>";
    }
    auto *il = new QLabel(info);
    il->setTextFormat(Qt::RichText);
    il->setWordWrap(true);
    sv->addWidget(il);

    const auto rows = impactRows(map.value("impacts").toObject());
    auto *tl = new QLabel(tr("<b>Impacts</b> (flooded features in this member's map)"));
    sv->addWidget(tl);
    auto *table = new QTableWidget(int(rows.size()), 2);
    table->setHorizontalHeaderLabels({tr("Feature"), tr("Flooded")});
    table->verticalHeader()->hide();
    table->setEditTriggers(QAbstractItemView::NoEditTriggers);
    table->setWordWrap(true);
    table->setTextElideMode(Qt::ElideNone);
    connect(table->horizontalHeader(), &QHeaderView::sectionResized, table, [table]() { table->resizeRowsToContents(); });
    table->horizontalHeader()->setSectionResizeMode(0, QHeaderView::ResizeToContents);
    table->horizontalHeader()->setStretchLastSection(true);
    for (int i = 0; i < rows.size(); ++i)
    {
        table->setItem(i, 0, new QTableWidgetItem(rows[i].first));
        auto *it = new QTableWidgetItem(rows[i].second);
        it->setToolTip(rows[i].second);
        table->setItem(i, 1, it);
    }
    table->resizeRowsToContents();
    sv->addWidget(table, 1);
    page->addWidget(side);
    page->setStretchFactor(0, 3);
    page->setStretchFactor(1, 2);

    // the map PNG: shown from the cache at once, fetched again (the service may replace a library map by the
    // 2D result under the same name)
    const QString png = map.value("png").toString();
    if (png.isEmpty()) { img->setMessage(tr("No map image for this member yet.")); return page; }
    const QString path = resolve(png);
    if (images_.contains(path)) img->setPixmap(images_.value(path));
    QPointer<FitImage> guard(img);
    ++pending_;
    data_->getBytes(path, [this, guard, path](const QByteArray &bytes, bool ok) {
        --pending_;
        QPixmap pm;
        if (ok && pm.loadFromData(bytes)) images_[path] = pm;
        if (!guard) return;
        if (!pm.isNull()) guard->setPixmap(pm);
        else if (!images_.contains(path)) guard->setMessage(tr("Map image not available:\n%1").arg(path));
    });
    return page;
}

QChartView *FloodAlertDialog::bandChart()
{
    const QJsonObject band = alert_.value("forecast").toObject().value("band").toObject();
    const QJsonArray tt = band.value("t_utc").toArray();
    if (tt.size() < 2) return nullptr;
    const QDateTime issued = parseUtc(alert_.value("issued_utc").toString());
    const QDateTime t0 = issued.isValid() ? issued : parseUtc(tt.first().toString());
    QVector<double> x;
    for (const QJsonValue &v : tt) x << t0.secsTo(parseUtc(v.toString())) / 3600.0;
    auto series = [&](const char *key) {
        QVector<double> y;
        const QJsonArray a = band.value(key).toArray();
        for (int i = 0; i < x.size(); ++i)
            y << (i < a.size() && a[i].isDouble() ? a[i].toDouble() * kCfsPerCms : std::numeric_limits<double>::quiet_NaN());
        return y;
    };
    const QVector<double> q5 = series("q5"), q25 = series("q25"), q50 = series("q50"), q75 = series("q75"), q95 = series("q95");
    double ymax = 0;
    for (const auto *s : {&q95, &q50})
        for (double y : *s) if (std::isfinite(y)) ymax = std::max(ymax, y);
    const double thr = alert_.value("threshold_cms").toDouble(std::numeric_limits<double>::quiet_NaN()) * kCfsPerCms;
    if (std::isfinite(thr)) ymax = std::max(ymax, thr);
    if (ymax <= 0) return nullptr;

    auto *chart = new QChart;
    chart->setTitle(tr("Forecast flow at the trigger reach (ensemble band)"));
    chart->setMargins(QMargins(4, 4, 4, 4));
    // x: hours from the issue time, labelled with the local clock time
    auto *ax = new QCategoryAxis;
    ax->setLabelsPosition(QCategoryAxis::AxisLabelsPositionOnValue);
    // a flash flood fills the first hours of a 24 h band: show up to 2 h after the upper band falls below a
    // quarter of the threshold (at least 6 h)
    const double x0 = x.first();
    double x1 = x.last();
    if (std::isfinite(thr))
    {
        int last = -1;
        for (int i = 0; i < x.size(); ++i) if (std::isfinite(q95[i]) && q95[i] >= 0.25 * thr) last = i;
        if (last >= 0) x1 = std::min(x.last(), std::max(x[last] + 2.0, x0 + 6.0));
    }
    if (x1 < x.last()) chart->setTitle(tr("Forecast flow at the trigger reach (ensemble band; first %1 h of %2 h shown)")
                                           .arg(x1 - x0, 0, 'f', 0).arg(x.last() - x0, 0, 'f', 0));
    ax->setRange(x0, x1);
    const double span = x1 - x0;
    const int step = span <= 8 ? 1 : span <= 26 ? 3 : span <= 72 ? 6 : 12;
    // ticks on local clock hours that are multiples of the step
    const QDateTime tStart = t0.addSecs(qint64(std::ceil(x0 * 3600)));
    QDateTime tk = QDateTime(tStart.date(), QTime(tStart.time().hour(), 0), QTimeZone::UTC);
    if (tk < tStart) tk = tk.addSecs(3600);
    for (int g = 0; g < 24 && easternWall(tk).time().hour() % step != 0; ++g) tk = tk.addSecs(3600);
    QString lastDay;
    const QLocale loc(QLocale::English, QLocale::UnitedStates);
    for (; t0.secsTo(tk) / 3600.0 <= x1 + 1e-9; tk = tk.addSecs(step * 3600))
    {
        const QDateTime e = easternWall(tk);
        QString lbl = loc.toString(e, "h AP");
        const QString day = loc.toString(e, "ddd");
        if (day != lastDay) { lbl = day + " " + lbl; lastDay = day; }
        ax->append(lbl, t0.secsTo(tk) / 3600.0);
    }
    ax->setTitleText(tr("Local time (%1)").arg(localTime(t0, false).section(' ', -1)));
    auto *ay = new QValueAxis;
    ay->setRange(0, ymax * 1.08);
    ay->applyNiceNumbers();
    ay->setLabelFormat("%.0f");
    ay->setTitleText(tr("Flow (cfs)"));
    chart->addAxis(ax, Qt::AlignBottom);
    chart->addAxis(ay, Qt::AlignLeft);

    auto line = [&](const QVector<double> &y) {
        auto *s = new QLineSeries;
        for (int i = 0; i < x.size(); ++i) if (std::isfinite(y[i])) s->append(x[i], y[i]);
        return s;
    };
    auto area = [&](const QVector<double> &lo, const QVector<double> &hi, const QColor &c, const QString &name) {
        auto *a = new QAreaSeries(line(hi), line(lo));
        a->setName(name);
        a->setColor(c);
        a->setBorderColor(Qt::transparent);
        chart->addSeries(a);
        a->attachAxis(ax);
        a->attachAxis(ay);
    };
    area(q5, q95, QColor(66, 133, 244, 60), tr("5-95 %"));
    area(q25, q75, QColor(66, 133, 244, 110), tr("25-75 %"));
    QLineSeries *med = line(q50);
    med->setName(tr("Median"));
    med->setPen(QPen(QColor("#1a4f9c"), 2));
    chart->addSeries(med);
    med->attachAxis(ax);
    med->attachAxis(ay);
    if (std::isfinite(thr))
    {
        auto *th = new QLineSeries;
        th->setName(tr("Threshold %1").arg(cfs(thr / kCfsPerCms, false)));
        th->append(x0, thr);
        th->append(x1, thr);
        th->setPen(QPen(QColor("#c53030"), 1.5, Qt::DashLine));
        chart->addSeries(th);
        th->attachAxis(ax);
        th->attachAxis(ay);
    }
    if (x0 <= 0 && x1 >= 0)
    {
        auto *now = new QLineSeries;
        now->setName(tr("Issued"));
        now->append(0, 0);
        now->append(0, ymax * 1.08);
        now->setPen(QPen(QColor("#666666"), 1, Qt::DotLine));
        chart->addSeries(now);
        now->attachAxis(ax);
        now->attachAxis(ay);
    }
    chart->legend()->setAlignment(Qt::AlignRight);
    auto *view = new QChartView(chart);
    view->setRenderHint(QPainter::Antialiasing);
    view->setMinimumHeight(230);
    return view;
}
