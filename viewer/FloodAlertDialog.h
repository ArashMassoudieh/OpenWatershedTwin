// OpenWatershedTwin viewer: flood-alert popup (docs/flood_service_design.md, "Alert file"; docs/viewer_data.md,
// "Flood alerts"). Non-modal and resizable (show(), never exec(): a nested event loop blocks the single-threaded
// WebAssembly build). Shows one site's alert: header (site, issue time, trigger statistic and threshold, state, job
// progress), one tab per mapped member (max-depth PNG rendered by the flood service, its source and stage, the
// impacts table), the forecast band at the trigger reach, and links (demo page, animations).
// Units in the UI: cfs and ft; times in America/New_York local time (EDT/EST).
#pragma once
#include <QDateTime>
#include <QDialog>
#include <QHash>
#include <QJsonObject>
#include <QPixmap>

class DataSource;
class QChartView;
class QHBoxLayout;
class QLabel;
class QProgressBar;
class QTabWidget;

namespace alertfmt
{
constexpr double kCfsPerCms = 35.3147;
QDateTime parseUtc(const QString &iso);                 // ISO 8601 ("...Z"); invalid on failure
QDateTime easternWall(const QDateTime &utc, QString *abbrev = nullptr);   // America/New_York wall clock
QString localTime(const QDateTime &utc, bool withDate = true);   // "Sat 19 Jul, 6:25 PM EDT"
QString cfs(double cms, bool round3 = true);            // "3,700 cfs" (3 significant figures, or exact)
QString duration(qint64 seconds);                       // "1 h 05 min"
QString stateText(const QString &state);                // "Watch", "Triggered", ...
QString memberLabel(const QString &member);             // "p90" -> "90th percentile member"
QColor stateColor(const QString &state);                // watch amber, triggered/mapping/mapped red
} // namespace alertfmt

// Scales a pixmap to fit, keeping the aspect ratio.
class FitImage : public QWidget
{
    Q_OBJECT
public:
    explicit FitImage(QWidget *parent = nullptr);
    void setPixmap(const QPixmap &p) { pix_ = p; update(); }
    void setMessage(const QString &m) { msg_ = m; update(); }
    QSize sizeHint() const override { return QSize(420, 560); }

protected:
    void paintEvent(QPaintEvent *) override;

private:
    QPixmap pix_;
    QString msg_;
};

class FloodAlertDialog : public QDialog
{
    Q_OBJECT
public:
    // outputs: the outputs prefix ("outputs/"); PNG and animation paths in the alert file are relative to it
    FloodAlertDialog(DataSource *data, const QString &outputs, QWidget *parent = nullptr);
    // (re)build from an alert document; does nothing when the document is unchanged
    void setAlert(const QJsonObject &alert);
    QString site() const { return alert_.value("site").toString(); }
    void selectMember(const QString &member);       // e.g. "p90" (tests)
    int pendingImages() const { return pending_; }

private:
    void rebuild();
    QWidget *memberPage(const QString &member, const QJsonObject &map);
    QChartView *bandChart();
    QString resolve(const QString &rel) const;      // a path under outputs/ (absolute URLs pass)

    DataSource *data_;
    QString outputs_;
    QJsonObject alert_;
    QLabel *header_ = nullptr;
    QProgressBar *progress_ = nullptr;
    QTabWidget *tabs_ = nullptr;
    QWidget *chartBox_ = nullptr;
    QHBoxLayout *links_ = nullptr;
    QHash<QString, QPixmap> images_;                // by resolved path (kept across refreshes)
    int pending_ = 0;
};
