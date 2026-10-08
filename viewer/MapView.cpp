#include "MapView.h"

#include <QContextMenuEvent>
#include <QFontMetrics>
#include <QImage>
#include <QJsonArray>
#include <QJsonObject>
#include <QMouseEvent>
#include <QNativeGestureEvent>
#include <QPainter>
#include <QToolTip>
#include <QWheelEvent>
#include <QtMath>
#include <cmath>
#include <limits>

// ------------------------------------------------------------------------------------------------ colour ramps
namespace
{
struct Stop { double x; QColor c; };

QColor ramp(const QVector<Stop> &stops, double x)
{
    if (x <= stops.front().x) return stops.front().c;
    if (x >= stops.back().x) return stops.back().c;
    for (int i = 1; i < stops.size(); ++i)
        if (x <= stops[i].x)
        {
            const double f = (x - stops[i - 1].x) / (stops[i].x - stops[i - 1].x);
            const QColor a = stops[i - 1].c, b = stops[i].c;
            return QColor::fromRgbF(a.redF() + f * (b.redF() - a.redF()), a.greenF() + f * (b.greenF() - a.greenF()),
                                    a.blueF() + f * (b.blueF() - a.blueF()));
        }
    return stops.back().c;
}

const QVector<Stop> &paletteStops(const QString &name)
{
    static const QVector<Stop> moisture = {{0, QColor("#a6611a")}, {0.35, QColor("#dfc27d")}, {0.6, QColor("#c7e9c0")},
                                           {0.8, QColor("#41b6c4")}, {1, QColor("#225ea8")}};
    static const QVector<Stop> depth = {{0, QColor("#f7fbff")}, {0.4, QColor("#9ecae1")}, {0.75, QColor("#4292c6")},
                                        {1, QColor("#08306b")}};
    static const QVector<Stop> flow = {{0, QColor("#ffffcc")}, {0.35, QColor("#a1dab4")}, {0.7, QColor("#41b6c4")},
                                       {1, QColor("#253494")}};
    static const QVector<Stop> head = {{0, QColor("#b2182b")}, {0.25, QColor("#ef8a62")}, {0.5, QColor("#f7f7f7")},
                                       {0.75, QColor("#67a9cf")}, {1, QColor("#2166ac")}};
    if (name == "depth") return depth;
    if (name == "flow") return flow;
    if (name == "head") return head;
    return moisture;
}

const QColor kNoData("#e4e2dc");
const QColor kBackground("#f4f3ef");
} // namespace

QColor ColorScale::color(double v) const
{
    if (!std::isfinite(v)) return kNoData;
    const double f = (max > min) ? (v - min) / (max - min) : 0.5;
    return ramp(paletteStops(palette), f);
}

// ------------------------------------------------------------------------------------------------ geometry
MapView::MapView(QWidget *parent) : QWidget(parent)
{
    setMouseTracking(true);
    setMinimumSize(320, 240);
    setAttribute(Qt::WA_AcceptTouchEvents);
}

QPointF MapView::project(double lon, double lat)
{
    return QPointF((lon - lon0_) * kx_, (lat - lat0_) * ky_);
}

static void collectCoords(const QJsonValue &v, double &x0, double &y0, double &x1, double &y1)
{
    const QJsonArray a = v.toArray();
    if (a.size() >= 2 && a[0].isDouble())
    {
        x0 = std::min(x0, a[0].toDouble()); x1 = std::max(x1, a[0].toDouble());
        y0 = std::min(y0, a[1].toDouble()); y1 = std::max(y1, a[1].toDouble());
        return;
    }
    for (const QJsonValue &c : a) collectCoords(c, x0, y0, x1, y1);
}

static void setOriginFrom(const QJsonDocument &doc, bool &set, double &lon0, double &lat0, double &kx, double &ky)
{
    if (set) return;
    double x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
    for (const QJsonValue &f : doc.object().value("features").toArray())
        collectCoords(f.toObject().value("geometry").toObject().value("coordinates"), x0, y0, x1, y1);
    if (x1 < x0) return;
    lon0 = 0.5 * (x0 + x1);
    lat0 = 0.5 * (y0 + y1);
    kx = 111320.0 * std::cos(qDegreesToRadians(lat0));
    ky = 110574.0;
    set = true;
}

QPainterPath MapView::ringsToPath(const QJsonArray &rings)
{
    QPainterPath p;
    for (const QJsonValue &r : rings)
    {
        const QJsonArray pts = r.toArray();
        for (int i = 0; i < pts.size(); ++i)
        {
            const QJsonArray c = pts[i].toArray();
            const QPointF w = project(c[0].toDouble(), c[1].toDouble());
            if (i == 0) p.moveTo(w); else p.lineTo(w);
        }
        p.closeSubpath();
    }
    return p;
}

void MapView::setUnits(const QJsonDocument &doc, const QString &idField, const QString &labelField)
{
    setOriginFrom(doc, originSet_, lon0_, lat0_, kx_, ky_);
    units_.clear();
    for (const QJsonValue &fv : doc.object().value("features").toArray())
    {
        const QJsonObject f = fv.toObject(), g = f.value("geometry").toObject(), pr = f.value("properties").toObject();
        MapFeature m;
        m.id = pr.value(idField).toVariant().toString();
        m.label = pr.value(labelField).toVariant().toString();
        m.path.setFillRule(Qt::OddEvenFill);
        const QString type = g.value("type").toString();
        if (type == "Polygon") m.path.addPath(ringsToPath(g.value("coordinates").toArray()));
        else if (type == "MultiPolygon")
            for (const QJsonValue &poly : g.value("coordinates").toArray()) m.path.addPath(ringsToPath(poly.toArray()));
        m.point = m.path.boundingRect().center();
        units_.push_back(m);
    }
    fitted_ = false;
    update();
}

void MapView::setReaches(const QJsonDocument &doc, const QString &idField, const QString &widthField)
{
    setOriginFrom(doc, originSet_, lon0_, lat0_, kx_, ky_);
    reaches_.clear();
    double wmax = 0;
    for (const QJsonValue &fv : doc.object().value("features").toArray())
    {
        const QJsonObject f = fv.toObject(), g = f.value("geometry").toObject(), pr = f.value("properties").toObject();
        MapFeature m;
        m.id = pr.value(idField).toVariant().toString();
        m.width = pr.value(widthField).toDouble(1.0);
        wmax = std::max(wmax, m.width);
        QJsonArray lines = g.value("coordinates").toArray();
        if (g.value("type").toString() == "LineString") lines = QJsonArray{lines};
        for (const QJsonValue &l : lines)
        {
            const QJsonArray pts = l.toArray();
            for (int i = 0; i < pts.size(); ++i)
            {
                const QJsonArray c = pts[i].toArray();
                const QPointF w = project(c[0].toDouble(), c[1].toDouble());
                if (i == 0) m.path.moveTo(w); else m.path.lineTo(w);
            }
        }
        reaches_.push_back(m);
    }
    for (MapFeature &m : reaches_) m.width = (wmax > 0) ? std::sqrt(m.width / wmax) : 1.0;   // 0..1
    update();
}

void MapView::setPoints(const QJsonDocument &doc, const QString &idField, const QString &labelField,
                        const QString &flagField)
{
    setOriginFrom(doc, originSet_, lon0_, lat0_, kx_, ky_);
    points_.clear();
    for (const QJsonValue &fv : doc.object().value("features").toArray())
    {
        const QJsonObject f = fv.toObject(), g = f.value("geometry").toObject(), pr = f.value("properties").toObject();
        if (g.value("type").toString() != "Point") continue;
        const QJsonArray c = g.value("coordinates").toArray();
        MapFeature m;
        m.id = pr.value(idField).toVariant().toString();
        m.label = pr.value(labelField).toVariant().toString();
        m.point = project(c[0].toDouble(), c[1].toDouble());
        m.flag = flagField.isEmpty() ? true : pr.value(flagField).toBool();
        points_.push_back(m);
    }
    update();
}

void MapView::setBoundary(const QJsonDocument &doc)
{
    setOriginFrom(doc, originSet_, lon0_, lat0_, kx_, ky_);
    boundary_ = QPainterPath();
    for (const QJsonValue &fv : doc.object().value("features").toArray())
    {
        const QJsonObject g = fv.toObject().value("geometry").toObject();
        if (g.value("type").toString() == "Polygon") boundary_.addPath(ringsToPath(g.value("coordinates").toArray()));
        else if (g.value("type").toString() == "MultiPolygon")
            for (const QJsonValue &poly : g.value("coordinates").toArray()) boundary_.addPath(ringsToPath(poly.toArray()));
    }
    update();
}

void MapView::setValues(const QHash<QString, double> &values, const ColorScale &scale)
{
    values_ = values;
    scale_ = scale;
    hasValues_ = true;
    update();
}

void MapView::setSelectedUnit(const QString &id)
{
    selected_ = id;
    update();
}

// ------------------------------------------------------------------------------------------------ view transform
QRectF MapView::worldExtent() const
{
    QRectF r;
    for (const MapFeature &m : units_) r = r.isNull() ? m.path.boundingRect() : r.united(m.path.boundingRect());
    if (!boundary_.isEmpty()) r = r.isNull() ? boundary_.boundingRect() : r.united(boundary_.boundingRect());
    return r;
}

void MapView::fitToExtent()
{
    const QRectF r = worldExtent();
    if (r.isNull() || width() < 10) return;
    ppm_ = 0.92 * std::min(width() / r.width(), height() / r.height());
    fitPpm_ = ppm_;
    centre_ = r.center();
    fitted_ = true;
    update();
}

QPointF MapView::toScreen(const QPointF &w) const
{
    return QPointF(width() / 2.0 + (w.x() - centre_.x()) * ppm_, height() / 2.0 - (w.y() - centre_.y()) * ppm_);
}

QPointF MapView::toWorld(const QPointF &s) const
{
    return QPointF(centre_.x() + (s.x() - width() / 2.0) / ppm_, centre_.y() - (s.y() - height() / 2.0) / ppm_);
}

void MapView::zoomAbout(const QPointF &screen, double factor)
{
    const QPointF w = toWorld(screen);
    ppm_ = std::clamp(ppm_ * factor, 1e-4, 2.0);
    // keep the world point under the cursor fixed
    centre_ = QPointF(w.x() - (screen.x() - width() / 2.0) / ppm_, w.y() + (screen.y() - height() / 2.0) / ppm_);
    update();
}

QString MapView::unitAt(const QPointF &screen) const
{
    const QPointF w = toWorld(screen);
    for (const MapFeature &m : units_)
        if (m.path.boundingRect().contains(w) && m.path.contains(w)) return m.id;
    return QString();
}

QString MapView::gageAt(const QPointF &screen) const
{
    for (const MapFeature &m : points_)
        if (m.flag && QLineF(toScreen(m.point), screen).length() < 10) return m.id;
    return QString();
}

// ------------------------------------------------------------------------------------------------ painting
void MapView::paintMap(QPainter &p, const QSize &size)
{
    p.fillRect(QRect(QPoint(0, 0), size), kBackground);
    p.setRenderHint(QPainter::Antialiasing);
    QTransform t;
    t.translate(size.width() / 2.0, size.height() / 2.0);
    t.scale(ppm_, -ppm_);
    t.translate(-centre_.x(), -centre_.y());

    if (!boundary_.isEmpty())
    {
        p.setPen(Qt::NoPen);
        p.setBrush(QColor("#ebe9e2"));
        p.drawPath(t.map(boundary_));
    }
    for (const MapFeature &m : units_)
    {
        const double v = values_.value(m.id, std::numeric_limits<double>::quiet_NaN());
        p.setBrush(hasValues_ ? scale_.color(v) : QColor("#dfe7d8"));
        p.setPen(QPen(QColor(90, 90, 90, 150), 0.7));
        p.drawPath(t.map(m.path));
    }
    for (const MapFeature &m : reaches_)
    {
        p.setBrush(Qt::NoBrush);
        p.setPen(QPen(QColor("#2b6cb0"), 0.8 + 3.2 * m.width, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
        p.drawPath(t.map(m.path));
    }
    for (const MapFeature &m : units_)
    {
        if (m.id != hover_ && m.id != selected_) continue;
        p.setBrush(Qt::NoBrush);
        p.setPen(QPen(m.id == selected_ ? QColor("#111111") : QColor("#444444"), m.id == selected_ ? 2.4 : 1.6));
        p.drawPath(t.map(m.path));
    }
    if (!boundary_.isEmpty())
    {
        p.setBrush(Qt::NoBrush);
        p.setPen(QPen(QColor("#333333"), 1.4));
        p.drawPath(t.map(boundary_));
    }
    // unit labels when the units are large enough on screen
    QFont f = p.font();
    f.setPointSizeF(7.5);
    p.setFont(f);
    for (const MapFeature &m : units_)
    {
        const QRectF br = t.mapRect(m.path.boundingRect());
        if (br.width() < 48 || br.height() < 24) continue;
        const QPointF c = t.map(m.point);
        p.setPen(QColor(40, 40, 40, 200));
        p.drawText(QRectF(c.x() - 40, c.y() - 8, 80, 16), Qt::AlignCenter, m.label);
    }
    // points: calibration gages as triangles, others (dams, other gages) as small squares
    for (const MapFeature &m : points_)
    {
        const QPointF c = t.map(m.point);
        if (m.flag)
        {
            QPolygonF tri;
            tri << c + QPointF(0, -8) << c + QPointF(7, 5) << c + QPointF(-7, 5);
            p.setBrush(QColor("#d94801"));
            p.setPen(QPen(Qt::white, 1.2));
            p.drawPolygon(tri);
        }
        else
        {
            p.setBrush(QColor("#636363"));
            p.setPen(QPen(Qt::white, 1));
            p.drawRect(QRectF(c.x() - 3.5, c.y() - 3.5, 7, 7));
        }
        if (m.flag || ppm_ > 2.5 * fitPpm_)          // other points: labels only when zoomed in
        {
            p.setPen(QColor("#222222"));
            p.drawText(c + QPointF(9, 4), m.label);
        }
    }
    paintLegend(p, size);
}

void MapView::paintLegend(QPainter &p, const QSize &size)
{
    if (!hasValues_) return;
    const QString title =
        scale_.label + (scale_.unit.isEmpty() || scale_.unit == "-" ? QString() : " (" + scale_.unit + ")");
    QFont tf = p.font();
    tf.setPointSizeF(8.5);
    const int w = std::max(220, QFontMetrics(tf).horizontalAdvance(title) + 20), h = 54, x = 12,
              y = size.height() - h - 12;
    p.setPen(QPen(QColor(0, 0, 0, 60)));
    p.setBrush(QColor(255, 255, 255, 230));
    p.drawRoundedRect(QRectF(x, y, w, h), 6, 6);
    QFont f = p.font();
    f.setPointSizeF(8.5);
    p.setFont(f);
    p.setPen(QColor("#222222"));
    p.drawText(QRectF(x + 8, y + 4, w - 16, 16), Qt::AlignLeft, title);
    const QRectF bar(x + 8, y + 22, w - 16, 10);
    for (int i = 0; i < bar.width(); ++i)
    {
        const double v = scale_.min + (scale_.max - scale_.min) * i / bar.width();
        p.setPen(scale_.color(v));
        p.drawLine(QPointF(bar.left() + i, bar.top()), QPointF(bar.left() + i, bar.bottom()));
    }
    p.setPen(QColor("#222222"));
    p.drawText(QRectF(bar.left(), bar.bottom() + 2, 80, 14), Qt::AlignLeft, QString::number(scale_.min, 'g', 3));
    p.drawText(QRectF(bar.right() - 80, bar.bottom() + 2, 80, 14), Qt::AlignRight, QString::number(scale_.max, 'g', 3));
}

void MapView::paintEvent(QPaintEvent *)
{
    if (!fitted_) fitToExtent();
    QPainter p(this);
    paintMap(p, size());
}

QImage MapView::renderImage(const QSize &size)
{
    resize(size);
    fitToExtent();
    QImage img(size, QImage::Format_ARGB32_Premultiplied);
    QPainter p(&img);
    paintMap(p, size);
    return img;
}

void MapView::resizeEvent(QResizeEvent *)
{
    if (!fitted_) fitToExtent();
}

// ------------------------------------------------------------------------------------------------ interaction
void MapView::wheelEvent(QWheelEvent *e)
{
    const double steps = e->angleDelta().y() / 120.0;
    if (steps != 0) zoomAbout(e->position(), std::pow(1.25, steps));
    e->accept();
}

void MapView::mousePressEvent(QMouseEvent *e)
{
    if (e->button() == Qt::LeftButton)
    {
        dragging_ = true;
        dragStart_ = e->position();
        centreAtDrag_ = centre_;
        setCursor(Qt::ClosedHandCursor);
    }
}

void MapView::mouseMoveEvent(QMouseEvent *e)
{
    if (dragging_)
    {
        const QPointF d = e->position() - dragStart_;
        centre_ = centreAtDrag_ - QPointF(d.x() / ppm_, -d.y() / ppm_);
        update();
        return;
    }
    const QString g = gageAt(e->position());
    const QString u = g.isEmpty() ? unitAt(e->position()) : QString();
    if (u != hover_)
    {
        hover_ = u;
        update();
    }
    if (!g.isEmpty())
    {
        for (const MapFeature &m : points_)
            if (m.id == g) QToolTip::showText(e->globalPosition().toPoint(), m.label + " (" + m.id + ")", this);
    }
    else if (!u.isEmpty())
    {
        const double v = values_.value(u, std::numeric_limits<double>::quiet_NaN());
        QString txt = u;
        if (hasValues_)
            txt += ": " + (std::isfinite(v) ? QString::number(v, 'g', 4) + " " + scale_.unit : QString("no data"));
        QToolTip::showText(e->globalPosition().toPoint(), txt, this);
    }
    else
        QToolTip::hideText();
}

void MapView::mouseReleaseEvent(QMouseEvent *e)
{
    if (e->button() == Qt::LeftButton && dragging_)
    {
        dragging_ = false;
        unsetCursor();
        if ((e->position() - dragStart_).manhattanLength() < 4)
        {
            const QString u = unitAt(e->position());
            if (!u.isEmpty()) emit unitClicked(u);
        }
    }
}

void MapView::mouseDoubleClickEvent(QMouseEvent *e)
{
    zoomAbout(e->position(), e->button() == Qt::RightButton ? 0.5 : 2.0);
}

void MapView::contextMenuEvent(QContextMenuEvent *e)
{
    const QString g = gageAt(e->pos());
    if (!g.isEmpty()) { emit gageContextMenu(g, e->globalPos()); return; }
    const QString u = unitAt(e->pos());
    if (!u.isEmpty()) emit unitContextMenu(u, e->globalPos());
}

bool MapView::event(QEvent *e)
{
    if (e->type() == QEvent::NativeGesture)
    {
        auto *g = static_cast<QNativeGestureEvent *>(e);
        if (g->gestureType() == Qt::ZoomNativeGesture)
        {
            zoomAbout(g->position(), 1.0 + g->value());
            return true;
        }
    }
    return QWidget::event(e);
}
