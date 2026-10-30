// OpenWatershedTwin viewer: fetches the twin's static files (docs/viewer_data.md) over HTTP (browser) or from a
// local folder (desktop).
#pragma once
#include <QJsonDocument>
#include <QNetworkAccessManager>
#include <QObject>
#include <QString>
#include <QUrl>
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
    // Binary files (PNG maps of the flood alerts): `done` gets the bytes and ok = false on failure.
    void getBytes(const QString &relativePath, std::function<void(const QByteArray &, bool)> done);
    // Absolute URL of a file under the base (a file:// URL on the desktop), e.g. to open it in the browser.
    // Absolute http(s) URLs pass through unchanged.
    QUrl urlFor(const QString &relativePath) const;

private:
    // raw fetch shared by getJson and getBytes: ok = false and an error message on failure
    void fetch(const QString &rel, std::function<void(const QByteArray &, bool, const QString &)> done);
    QString base_;
    bool local_ = false;
    QNetworkAccessManager nam_;
};
