// OpenWatershedTwin viewer: main window. Map in the centre, element buttons on top, time slider below, chart
// panel on the right. Everything is driven by viewer_config.json and the twin's output files
// (docs/viewer_data.md).
#pragma once
#include "ChartPanel.h"

#include <QHash>
#include <QJsonArray>
#include <QJsonObject>
#include <QMainWindow>
#include <functional>

class DataSource;
class MapView;
class QButtonGroup;
class QComboBox;
class QLabel;
class QSlider;
class QTimer;
class QToolButton;

class MainWindow : public QMainWindow
{
    Q_OBJECT
public:
    explicit MainWindow(DataSource *data, QWidget *parent = nullptr);
    MapView *map() const { return map_; }
    // load everything; `ready` is called once the layers and map state are in
    void load(std::function<void()> ready = nullptr);
    enum class ChartScope { Variable, Element, All };   // the map variable, all of the element's, everything
    void showUnit(const QString &unit, TimeMode mode, ChartScope scope);
    void showGage(const QString &gage);
    void selectElement(const QString &elementId);

private slots:
    void onElementChanged();
    void onTimeChanged(int index);
    void togglePlay();
    void refresh();

private:
    void buildUi();
    void buildElementButtons();
    void loadOutputs(std::function<void()> ready);
    void updateMap();
    QString currentKey() const;          // "<element>:<variable>"
    QJsonObject currentVariable() const;
    void withUnitDoc(const QString &unit, std::function<void(const QJsonObject &)> f);
    void error(const QString &msg);

    DataSource *data_;
    MapView *map_ = nullptr;
    ChartPanel *charts_ = nullptr;
    QButtonGroup *elementGroup_ = nullptr;
    QWidget *elementBar_ = nullptr;
    QComboBox *variableBox_ = nullptr;
    QSlider *slider_ = nullptr;
    QLabel *timeLabel_ = nullptr, *statusLabel_ = nullptr;
    QToolButton *playButton_ = nullptr;
    QTimer *playTimer_ = nullptr, *refreshTimer_ = nullptr;

    QJsonObject config_, status_, mapState_;
    QJsonArray elements_;
    QString outputs_ = "outputs/";
    QHash<QString, QJsonObject> unitDocs_;
    QString selectedUnit_;
    ChartScope selectedScope_ = ChartScope::Variable;
    int pendingLayers_ = 0;
};
