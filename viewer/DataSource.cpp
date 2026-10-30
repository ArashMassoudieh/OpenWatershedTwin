#include "DataSource.h"

#include <QDir>
#include <QFile>
#include <QNetworkReply>
#include <QDateTime>
#include <QNetworkRequest>
#include <QTimer>
#include <QUrl>

DataSource::DataSource(const QString &base, QObject *parent) : QObject(parent), base_(base)
{
    local_ = !(base.startsWith("http://") || base.startsWith("https://"));
    if (!base_.endsWith('/')) base_ += '/';
}

QUrl DataSource::urlFor(const QString &rel) const
{
    if (rel.startsWith("http://") || rel.startsWith("https://")) return QUrl(rel);
    if (local_) return QUrl::fromLocalFile(QDir(base_).absoluteFilePath(rel));
    return QUrl(base_).resolved(QUrl(rel));
}

void DataSource::fetch(const QString &rel, std::function<void(const QByteArray &, bool, const QString &)> done)
{
    if (local_)
    {
        // deliver asynchronously as the network path does, so callers behave the same
        const QString path = QDir(base_).filePath(rel);
        QTimer::singleShot(0, this, [path, done]() {
            QFile f(path);
            if (!f.open(QIODevice::ReadOnly)) { done(QByteArray(), false, "cannot open " + path); return; }
            done(f.readAll(), true, path);
        });
        return;
    }
    // a unique query string: the browser's HTTP cache ignores CacheLoadControlAttribute in the WebAssembly build,
    // and the outputs change every cycle under the same names
    QUrl url = QUrl(base_).resolved(QUrl(rel));
    url.setQuery("v=" + QString::number(QDateTime::currentMSecsSinceEpoch()));
    QNetworkRequest req(url);
    req.setAttribute(QNetworkRequest::CacheLoadControlAttribute, QNetworkRequest::AlwaysNetwork);
    QNetworkReply *reply = nam_.get(req);
    connect(reply, &QNetworkReply::finished, this, [reply, rel, done]() {
        reply->deleteLater();
        if (reply->error() != QNetworkReply::NoError) { done(QByteArray(), false, rel + ": " + reply->errorString()); return; }
        done(reply->readAll(), true, rel);
    });
}

void DataSource::getJson(const QString &rel, std::function<void(const QJsonDocument &, const QString &)> done)
{
    fetch(rel, [done](const QByteArray &bytes, bool ok, const QString &what) {
        if (!ok) { done(QJsonDocument(), what); return; }
        QJsonParseError pe;
        const QJsonDocument doc = QJsonDocument::fromJson(bytes, &pe);
        done(doc, doc.isNull() ? what + ": " + pe.errorString() : QString());
    });
}

void DataSource::getBytes(const QString &rel, std::function<void(const QByteArray &, bool)> done)
{
    fetch(rel, [done](const QByteArray &bytes, bool ok, const QString &) { done(bytes, ok); });
}
