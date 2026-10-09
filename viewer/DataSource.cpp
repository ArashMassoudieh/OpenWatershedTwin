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

void DataSource::getJson(const QString &rel, std::function<void(const QJsonDocument &, const QString &)> done)
{
    if (local_)
    {
        // deliver asynchronously as the network path does, so callers behave the same
        const QString path = QDir(base_).filePath(rel);
        QTimer::singleShot(0, this, [path, done]() {
            QFile f(path);
            if (!f.open(QIODevice::ReadOnly)) { done(QJsonDocument(), "cannot open " + path); return; }
            QJsonParseError pe;
            const QJsonDocument doc = QJsonDocument::fromJson(f.readAll(), &pe);
            done(doc, doc.isNull() ? path + ": " + pe.errorString() : QString());
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
        if (reply->error() != QNetworkReply::NoError) { done(QJsonDocument(), rel + ": " + reply->errorString()); return; }
        QJsonParseError pe;
        const QJsonDocument doc = QJsonDocument::fromJson(reply->readAll(), &pe);
        done(doc, doc.isNull() ? rel + ": " + pe.errorString() : QString());
    });
}
