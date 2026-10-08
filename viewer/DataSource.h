// OpenWatershedTwin viewer: fetches the twin's static files (docs/viewer_data.md) over HTTP (browser) or from a
// local folder (desktop).
#pragma once
#include <QJsonDocument>
#include <QNetworkAccessManager>
#include <QObject>
#include <QString>
#include <functional>

class DataSource : public QObject
{
    Q_OBJECT
public:
    // base: "http(s)://host/path/" or a local directory
    explicit DataSource(const QString &base, QObject *parent = nullptr);
    QString base() const { return base_; }
    // Calls `done` with the parsed document (null document and an error message on failure).
    void getJson(const QString &relativePath, std::function<void(const QJsonDocument &, const QString &)> done);

private:
    QString base_;
    bool local_ = false;
    QNetworkAccessManager nam_;
};
