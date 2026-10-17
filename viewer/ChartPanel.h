// OpenWatershedTwin viewer: time-series panel (QtCharts) for a unit or a gage: rainfall on top, one chart per
// selected series, the forecast period shaded and "now" marked; CSV export of what is shown.
// Optional band: a series with "lo"/"hi" arrays (e.g. the 95 % parameter-uncertainty band of an ensemble) is shaded
// between them when the "95% intervals" box is checked (unchecked by default; disabled when nothing shown has one).
#pragma once
#include <QJsonObject>
#include <QStringList>
#include <QWidget>

class QVBoxLayout;
class QLabel;
class QCheckBox;

enum class TimeMode { History, Forecast, Both };

class ChartPanel : public QWidget
{
    Q_OBJECT
public:
    explicit ChartPanel(QWidget *parent = nullptr);
    // doc: units/<id>.json or gages/<id>.json; keys: series to plot (rain is added on top when present)
    void show(const QString &title, const QJsonObject &doc, const QStringList &keys, TimeMode mode);
    void clear(const QString &message);
    void setIntervalText(const QString &label, const QString &tooltip);

private slots:
    void exportCsv();

private:
    QVBoxLayout *charts_ = nullptr;
    QLabel *title_ = nullptr;
    QCheckBox *intervals_ = nullptr;
    QString intervalTip_;
    QJsonObject doc_;
    QStringList keys_;
    TimeMode mode_ = TimeMode::Both;
    QString name_;
};
