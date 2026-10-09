// OpenWatershedTwin viewer: map widget drawn from GeoJSON layers with QPainter.
//
// Coordinates: WGS84 lon/lat projected to a local flat (equirectangular) system in metres about the watershed
// centre, which is accurate to well under a metre at watershed scale. View: a scale (pixels per metre) and the
// world point at the widget centre. Wheel zooms about the cursor, left-drag pans, double-click zooms in;
// right-click reports the feature under the cursor.
#pragma once
#include <QColor>
#include <QHash>
#include <QJsonDocument>
#include <QPainterPath>
#include <QPointF>
#include <QString>
#include <QStringList>
#include <QVector>
#include <QWidget>

struct MapFeature
{
    QString id, label;
    QPainterPath path;          // world coordinates (polygons and lines)
    QPointF point;              // world coordinates (points, label anchor)
    double width = 1;           // reaches: drawing weight
    bool flag = false;          // points: gage filter
};

struct ColorScale
{
    QString palette = "moisture";
    double min = 0, max = 1;
    QString label, unit;
    bool onReaches = false;     // colour the stream lines (values keyed by reach id), not the units
    bool log = false;           // logarithmic scale (flows); needs min > 0
    QColor color(double v) const;
};

class MapView : public QWidget
{
    Q_OBJECT
public:
    explicit MapView(QWidget *parent = nullptr);

    // layer loaders (GeoJSON documents); the first one loaded fixes the projection origin
    void setUnits(const QJsonDocument &doc, const QString &idField, const QString &labelField);
    void setReaches(const QJsonDocument &doc, const QString &idField, const QString &widthField);
    void setPoints(const QJsonDocument &doc, const QString &idField, const QString &labelField,
                   const QString &flagField);
    void setBoundary(const QJsonDocument &doc);
    void addMask(const QJsonDocument &doc, const QString &label);   // hatched areas outside the model

    // colouring of the units: value per unit id (NaN = no data)
    void setValues(const QHash<QString, double> &values, const ColorScale &scale);
    void setSelectedUnit(const QString &id);
    void fitToExtent();
    QImage renderImage(const QSize &size);   // off-screen rendering (screenshots)

signals:
    void unitContextMenu(const QString &unitId, const QPoint &globalPos);
    void gageContextMenu(const QString &gageId, const QPoint &globalPos);
    void unitClicked(const QString &unitId);

protected:
    void paintEvent(QPaintEvent *) override;
    void wheelEvent(QWheelEvent *) override;
    void mousePressEvent(QMouseEvent *) override;
    void mouseMoveEvent(QMouseEvent *) override;
    void mouseReleaseEvent(QMouseEvent *) override;
    void mouseDoubleClickEvent(QMouseEvent *) override;
    void contextMenuEvent(QContextMenuEvent *) override;
    void resizeEvent(QResizeEvent *) override;
    bool event(QEvent *) override;           // pinch zoom on touch screens

private:
    QPointF project(double lon, double lat);
    QPainterPath ringsToPath(const QJsonArray &rings);
    QPointF toScreen(const QPointF &w) const;
    QPointF toWorld(const QPointF &s) const;
    QString unitAt(const QPointF &screen) const;
    QString gageAt(const QPointF &screen) const;
    void zoomAbout(const QPointF &screen, double factor);
    void paintMap(QPainter &p, const QSize &size);
    void paintLegend(QPainter &p, const QSize &size);
    QRectF worldExtent() const;

    bool originSet_ = false;
    double lon0_ = 0, lat0_ = 0, kx_ = 1, ky_ = 1;
    QVector<MapFeature> units_, reaches_, points_;
    QPainterPath boundary_;
    struct Mask { QPainterPath path; QString label; };
    QVector<Mask> masks_;
    QString maskAt(const QPointF &screen) const;
    QHash<QString, double> values_;
    ColorScale scale_;
    bool hasValues_ = false;
    QString hover_, selected_;
    double ppm_ = 0.01;                      // pixels per metre
    double fitPpm_ = 0.01;                   // scale of the whole-watershed view
    QPointF centre_;                         // world point at the widget centre
    bool dragging_ = false, fitted_ = false;
    QPointF dragStart_, centreAtDrag_;
};
