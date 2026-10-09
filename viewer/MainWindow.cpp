#include "MainWindow.h"

#include "DataSource.h"
#include "MapView.h"

#include <QAction>
#include <QButtonGroup>
#include <QComboBox>
#include <QDateTime>
#include <QHBoxLayout>
#include <QLabel>
#include <QMenu>
#include <QDesktopServices>
#include <QUrl>
#include <QMessageBox>
#include <QSlider>
#include <QSplitter>
#include <QStatusBar>
#include <QTimeZone>
#include <QTimer>
#include <QToolBar>
#include <QToolButton>
#include <QVBoxLayout>
#include <cmath>
#include <limits>

namespace
{
QString dateText(double serial)
{
    return QDateTime::fromMSecsSinceEpoch(qint64((serial - 25569.0) * 86400000.0), QTimeZone::UTC)
        .toString("ddd d MMM yyyy, HH:mm 'UTC'");
}
} // namespace

MainWindow::MainWindow(DataSource *data, QWidget *parent) : QMainWindow(parent), data_(data)
{
    buildUi();
}

void MainWindow::buildUi()
{
    auto *tb = addToolBar(tr("Elements"));
    tb->setMovable(false);
    elementBar_ = new QWidget;
    auto *eb = new QHBoxLayout(elementBar_);
    eb->setContentsMargins(0, 0, 0, 0);
    eb->setSpacing(2);
    elementGroup_ = new QButtonGroup(this);
    elementGroup_->setExclusive(true);
    connect(elementGroup_, &QButtonGroup::buttonClicked, this, &MainWindow::onElementChanged);
    tb->addWidget(elementBar_);
    tb->addSeparator();
    variableBox_ = new QComboBox;
    variableBox_->setMinimumWidth(160);
    connect(variableBox_, &QComboBox::currentIndexChanged, this, [this](int) { updateMap(); });
    tb->addWidget(variableBox_);
    tb->addSeparator();
    tb->addAction(tr("Fit"), this, [this]() { map_->fitToExtent(); });
    tb->addAction(tr("Refresh"), this, &MainWindow::refresh);
    tb->addSeparator();
    linkBar_ = tb;

    auto *central = new QWidget;
    auto *v = new QVBoxLayout(central);
    v->setContentsMargins(0, 0, 0, 0);
    auto *split = new QSplitter(Qt::Horizontal);
    map_ = new MapView;
    charts_ = new ChartPanel;
    split->addWidget(map_);
    split->addWidget(charts_);
    split->setStretchFactor(0, 3);
    split->setStretchFactor(1, 2);
    v->addWidget(split, 1);

    auto *timeBar = new QHBoxLayout;
    timeBar->setContentsMargins(8, 4, 8, 4);
    playButton_ = new QToolButton;
    playButton_->setText(tr("Play"));
    connect(playButton_, &QToolButton::clicked, this, &MainWindow::togglePlay);
    slider_ = new QSlider(Qt::Horizontal);
    connect(slider_, &QSlider::valueChanged, this, &MainWindow::onTimeChanged);
    timeLabel_ = new QLabel;
    timeLabel_->setMinimumWidth(300);
    timeBar->addWidget(playButton_);
    timeBar->addWidget(slider_, 1);
    timeBar->addWidget(timeLabel_);
    v->addLayout(timeBar);
    setCentralWidget(central);

    statusLabel_ = new QLabel;
    statusBar()->addWidget(statusLabel_, 1);

    playTimer_ = new QTimer(this);
    playTimer_->setInterval(600);
    connect(playTimer_, &QTimer::timeout, this, [this]() {
        slider_->setValue(slider_->value() < slider_->maximum() ? slider_->value() + 1 : slider_->minimum());
    });
    refreshTimer_ = new QTimer(this);
    refreshTimer_->setInterval(15 * 60 * 1000);
    connect(refreshTimer_, &QTimer::timeout, this, &MainWindow::refresh);

    connect(map_, &MapView::unitContextMenu, this, [this](const QString &unit, const QPoint &pos) {
        // popup(), not exec(): a nested event loop blocks the single-threaded WebAssembly build
        auto *m = new QMenu(this);
        m->setAttribute(Qt::WA_DeleteOnClose);
        m->addSection(unit);
        const QString var = currentVariable().value("label").toString();
        const QString elabel = currentVariable().value("element_label").toString();
        m->addAction(tr("%1: history and forecast").arg(var), this, [this, unit]() { showUnit(unit, TimeMode::Both, ChartScope::Variable); });
        m->addAction(tr("%1: history").arg(var), this, [this, unit]() { showUnit(unit, TimeMode::History, ChartScope::Variable); });
        m->addAction(tr("%1: forecast").arg(var), this, [this, unit]() { showUnit(unit, TimeMode::Forecast, ChartScope::Variable); });
        m->addSeparator();
        m->addAction(tr("%1: all variables").arg(elabel), this, [this, unit]() { showUnit(unit, TimeMode::Both, ChartScope::Element); });
        m->addAction(tr("All elements of %1").arg(unit), this, [this, unit]() { showUnit(unit, TimeMode::Both, ChartScope::All); });
        m->addAction(tr("All elements, forecast"), this, [this, unit]() { showUnit(unit, TimeMode::Forecast, ChartScope::All); });
        m->popup(pos);
    });
    connect(map_, &MapView::unitClicked, this, [this](const QString &unit) { showUnit(unit, TimeMode::Both, ChartScope::Variable); });
    connect(map_, &MapView::gageContextMenu, this, [this](const QString &gage, const QPoint &pos) {
        auto *m = new QMenu(this);
        m->setAttribute(Qt::WA_DeleteOnClose);
        m->addSection(gage);
        m->addAction(tr("Flow and stage: model and USGS"), this, [this, gage]() { showGage(gage); });
        m->popup(pos);
    });
    resize(1400, 860);
}

void MainWindow::error(const QString &msg)
{
    statusLabel_->setText(tr("Error: %1").arg(msg));
}

void MainWindow::load(std::function<void()> ready)
{
    data_->getJson("viewer_config.json", [this, ready](const QJsonDocument &doc, const QString &err) {
        if (!err.isEmpty()) { error(err); return; }
        config_ = doc.object();
        setWindowTitle(config_.value("title").toString("OpenWatershedTwin"));
        // "links": documentation etc., opened in the browser (a new tab in the WebAssembly build)
        for (QAction *a : linkActions_) { linkBar_->removeAction(a); a->deleteLater(); }
        linkActions_.clear();
        for (const QJsonValue &lv : config_.value("links").toArray())
        {
            const QJsonObject l = lv.toObject();
            const QUrl url(l.value("url").toString());
            QAction *a = linkBar_->addAction(l.value("label").toString(), this, [url]() { QDesktopServices::openUrl(url); });
            a->setToolTip(l.value("tooltip").toString(url.toString()));
            linkActions_ << a;
        }
        outputs_ = config_.value("outputs").toString("outputs/");
        elements_ = config_.value("elements").toArray();
        buildElementButtons();
        const QJsonObject layers = config_.value("layers").toObject();
        pendingLayers_ = 0;
        auto layerDone = [this, ready]() {
            if (--pendingLayers_ == 0) loadOutputs(ready);
        };
        for (const QJsonValue &mv : layers.value("masks").toArray())
        {
            ++pendingLayers_;
            const QJsonObject l = mv.toObject();
            data_->getJson(l.value("url").toString(), [this, l, layerDone](const QJsonDocument &d, const QString &e) {
                if (!e.isEmpty()) error(e);
                else map_->addMask(d, l.value("label").toString());
                layerDone();
            });
        }
        for (const QString &name : {QString("boundary"), QString("units"), QString("reaches"), QString("points")})
        {
            if (!layers.contains(name)) continue;
            ++pendingLayers_;
            const QJsonObject l = layers.value(name).toObject();
            data_->getJson(l.value("url").toString(), [this, name, l, layerDone](const QJsonDocument &d, const QString &e) {
                if (!e.isEmpty()) error(e);
                else if (name == "units")
                    map_->setUnits(d, l.value("id_field").toString("id"), l.value("label_field").toString("id"));
                else if (name == "reaches")
                    map_->setReaches(d, l.value("id_field").toString("id"), l.value("width_field").toString());
                else if (name == "points")
                    map_->setPoints(d, l.value("id_field").toString("id"), l.value("label_field").toString("id"),
                                    l.value("gage_filter_field").toString());
                else
                    map_->setBoundary(d);
                layerDone();
            });
        }
        refreshTimer_->start();
    });
}

void MainWindow::buildElementButtons()
{
    for (QAbstractButton *b : elementGroup_->buttons()) { elementGroup_->removeButton(b); delete b; }
    int i = 0;
    for (const QJsonValue &ev : elements_)
    {
        const QJsonObject e = ev.toObject();
        auto *b = new QToolButton;
        b->setText(e.value("label").toString());
        b->setCheckable(true);
        b->setProperty("element", e.value("id").toString());
        b->setToolButtonStyle(Qt::ToolButtonTextOnly);
        elementGroup_->addButton(b, i++);
        elementBar_->layout()->addWidget(b);
    }
    // default: the first soil layer, else the first element
    QAbstractButton *def = elementGroup_->buttons().value(0);
    for (QAbstractButton *b : elementGroup_->buttons())
        if (b->property("element").toString() == "Soil_1") def = b;
    if (def) { def->setChecked(true); onElementChanged(); }
}

void MainWindow::selectElement(const QString &elementId)
{
    for (QAbstractButton *b : elementGroup_->buttons())
        if (b->property("element").toString() == elementId) { b->setChecked(true); onElementChanged(); }
}

void MainWindow::onElementChanged()
{
    QAbstractButton *b = elementGroup_->checkedButton();
    if (!b) return;
    const QString id = b->property("element").toString();
    QSignalBlocker block(variableBox_);
    variableBox_->clear();
    for (const QJsonValue &ev : elements_)
        if (ev.toObject().value("id").toString() == id)
        {
            // the map lists the variables marked "map": true (all of them when none is marked);
            // the charts show every variable
            const QJsonArray vars = ev.toObject().value("variables").toArray();
            bool anyMarked = false;
            for (const QJsonValue &vv : vars) anyMarked |= vv.toObject().contains("map");
            for (const QJsonValue &vv : vars)
                if (!anyMarked || vv.toObject().value("map").toBool())
                    variableBox_->addItem(vv.toObject().value("label").toString(), vv.toObject().value("id").toString());
        }
    variableBox_->setEnabled(variableBox_->count() > 1);
    updateMap();
    if (!selectedUnit_.isEmpty() && unitDocs_.contains(selectedUnit_) && selectedScope_ != ChartScope::All)
        showUnit(selectedUnit_, TimeMode::Both, selectedScope_);
}

QString MainWindow::currentKey() const
{
    QAbstractButton *b = elementGroup_->checkedButton();
    if (!b) return QString();
    return b->property("element").toString() + ":" + variableBox_->currentData().toString();
}

QJsonObject MainWindow::currentVariable() const
{
    QAbstractButton *b = elementGroup_->checkedButton();
    if (!b) return QJsonObject();
    const QString id = b->property("element").toString(), var = variableBox_->currentData().toString();
    for (const QJsonValue &ev : elements_)
        if (ev.toObject().value("id").toString() == id)
            for (const QJsonValue &vv : ev.toObject().value("variables").toArray())
                if (vv.toObject().value("id").toString() == var)
                {
                    QJsonObject o = vv.toObject();
                    o["element_label"] = ev.toObject().value("label").toString();
                    o["map_layer"] = ev.toObject().value("map_layer").toString("units");
                    return o;
                }
    return QJsonObject();
}

void MainWindow::loadOutputs(std::function<void()> ready)
{
    data_->getJson(outputs_ + "status.json", [this](const QJsonDocument &d, const QString &) {
        status_ = d.object();
        QString s = tr("Forecast issued %1").arg(status_.value("issued_utc").toString());
        if (status_.contains("rain_source")) s += "  |  " + tr("Rain: %1").arg(status_.value("rain_source").toString());
        if (status_.value("stale").toBool()) s += "  |  " + tr("STALE: the last cycle failed");
        statusText_ = s;
        statusLabel_->setText(s);
    });
    data_->getJson(outputs_ + "map_state.json", [this, ready](const QJsonDocument &d, const QString &err) {
        if (!err.isEmpty()) { error(err); if (ready) ready(); return; }
        mapState_ = d.object();
        const QJsonArray times = mapState_.value("times").toArray();
        const double now = mapState_.value("now").toDouble();
        int iNow = 0;
        double best = 1e18;
        for (int i = 0; i < times.size(); ++i)
            if (std::fabs(times[i].toDouble() - now) < best) { best = std::fabs(times[i].toDouble() - now); iNow = i; }
        QSignalBlocker block(slider_);
        slider_->setRange(0, std::max(0, int(times.size()) - 1));
        slider_->setValue(iNow);
        onTimeChanged(iNow);
        if (ready) ready();
    });
}

void MainWindow::updateMap()
{
    const QString key = currentKey();
    const QJsonObject var = currentVariable();
    const QJsonObject vals = mapState_.value("variables").toObject().value(key).toObject();
    if (vals.isEmpty() && !mapState_.isEmpty())        // e.g. a variable added to the config since the last cycle
        statusLabel_->setText(tr("No map data for %1 yet: it appears after the twin's next update.")
                                  .arg(currentVariable().value("label").toString()));
    else if (!statusText_.isEmpty())
        statusLabel_->setText(statusText_);
    const int i = slider_->value();
    QHash<QString, double> values;
    double lo = std::numeric_limits<double>::infinity(), hi = -lo;
    for (auto it = vals.constBegin(); it != vals.constEnd(); ++it)
    {
        const QJsonArray a = it.value().toArray();
        for (const QJsonValue &x : a)
            if (x.isDouble()) { lo = std::min(lo, x.toDouble()); hi = std::max(hi, x.toDouble()); }
        const QJsonValue x = a.at(i);
        values[it.key()] = x.isDouble() ? x.toDouble() : std::numeric_limits<double>::quiet_NaN();
    }
    ColorScale cs;
    cs.palette = var.value("palette").toString("moisture");
    cs.min = var.contains("min") ? var.value("min").toDouble() : (std::isfinite(lo) ? lo : 0.0);
    cs.max = var.contains("max") ? var.value("max").toDouble() : (std::isfinite(hi) ? hi : 1.0);
    cs.label = var.value("element_label").toString() + ": " + var.value("label").toString();
    cs.unit = var.value("unit").toString();
    cs.onReaches = var.value("map_layer").toString() == "reaches";
    cs.log = var.value("log").toBool();
    if (cs.log)                                      // log scale: lower end from the positive data
    {
        double pmin = std::numeric_limits<double>::infinity();
        for (auto it = vals.constBegin(); it != vals.constEnd(); ++it)
            for (const QJsonValue &x : it.value().toArray())
                if (x.isDouble() && x.toDouble() > 0) pmin = std::min(pmin, x.toDouble());
        if (!var.contains("min")) cs.min = std::isfinite(pmin) ? std::max(pmin, cs.max * 1e-4) : 1e-3;
    }
    map_->setValues(values, cs);
}

void MainWindow::onTimeChanged(int index)
{
    const QJsonArray times = mapState_.value("times").toArray();
    if (index < 0 || index >= times.size()) return;
    const double t = times[index].toDouble(), now = mapState_.value("now").toDouble();
    QString tag = std::fabs(t - now) < 1e-3 ? tr("now") : (t > now ? tr("forecast") : tr("past"));
    timeLabel_->setText(dateText(t) + "  (" + tag + ")");
    updateMap();
}

void MainWindow::togglePlay()
{
    if (playTimer_->isActive()) { playTimer_->stop(); playButton_->setText(tr("Play")); }
    else { playTimer_->start(); playButton_->setText(tr("Pause")); }
}

void MainWindow::refresh()
{
    unitDocs_.clear();
    loadOutputs(nullptr);
}

void MainWindow::withUnitDoc(const QString &unit, std::function<void(const QJsonObject &)> f)
{
    if (unitDocs_.contains(unit)) { f(unitDocs_.value(unit)); return; }
    data_->getJson(outputs_ + "units/" + unit + ".json", [this, unit, f](const QJsonDocument &d, const QString &err) {
        if (!err.isEmpty()) { error(err); return; }
        unitDocs_[unit] = d.object();
        f(d.object());
    });
}

void MainWindow::showUnit(const QString &unit, TimeMode mode, ChartScope scope)
{
    selectedUnit_ = unit;
    selectedScope_ = scope;
    map_->setSelectedUnit(unit);
    withUnitDoc(unit, [this, unit, mode, scope](const QJsonObject &doc) {
        QStringList keys;
        const QString element = elementGroup_->checkedButton() ? elementGroup_->checkedButton()->property("element").toString() : QString();
        if (scope == ChartScope::Variable)
            keys << currentKey();
        else
            for (const QJsonValue &ev : elements_)
                if (scope == ChartScope::All || ev.toObject().value("id").toString() == element)
                    for (const QJsonValue &vv : ev.toObject().value("variables").toArray())
                        keys << ev.toObject().value("id").toString() + ":" + vv.toObject().value("id").toString();
        const QString elabel = currentVariable().value("element_label").toString();
        const QString what = scope == ChartScope::All ? tr("all elements")
                             : scope == ChartScope::Element ? tr("%1, all variables").arg(elabel)
                                                            : elabel;
        const QString when = mode == TimeMode::History ? tr("history") : mode == TimeMode::Forecast ? tr("forecast")
                                                                                                    : tr("history and forecast");
        charts_->show(QString("%1: %2, %3").arg(unit, what, when), doc, keys, mode);
    });
}

void MainWindow::showGage(const QString &gage)
{
    data_->getJson(outputs_ + "gages/" + gage + ".json", [this, gage](const QJsonDocument &d, const QString &err) {
        if (!err.isEmpty()) { charts_->clear(tr("No model output at gage %1.").arg(gage)); return; }
        const QJsonObject o = d.object();
        charts_->show(QString("%1 (%2)").arg(o.value("name").toString(), gage), o, {"Q_model", "H_model"},
                      TimeMode::Both);
    });
}
