// OpenWatershedTwin viewer: entry point.
//   browser (WebAssembly): data are fetched relative to the page URL (the twin's web folder)
//   desktop: OWTViewer [--base <folder or URL>] [--screenshot out.png [--element Soil_2] [--unit SC30]]
//            [--test-alert-dialog dialog.png [--alert-member p90]]   (renders the first flood alert's popup)
#include <QCheckBox>
#include "DataSource.h"
#include "FloodAlertDialog.h"
#include "MainWindow.h"
#include "MapView.h"

#include <QApplication>
#include <QCommandLineParser>
#include <QDir>
#include <QTimer>
#include <memory>

#ifdef Q_OS_WASM
#include <emscripten/val.h>
#endif

int main(int argc, char *argv[])
{
    QApplication app(argc, argv);
    QApplication::setApplicationName("OWTViewer");

    QString base;
#ifdef Q_OS_WASM
    // the page's folder: origin + path, without query string or fragment
    const emscripten::val loc = emscripten::val::global("location");
    base = QString::fromStdString(loc["origin"].as<std::string>() + loc["pathname"].as<std::string>());
    base = base.left(base.lastIndexOf('/') + 1);
#endif
    QCommandLineParser cl;
    cl.addOption({"base", "Folder or URL with viewer_config.json", "base"});
    cl.addOption({"screenshot", "Render the window to this PNG and exit", "file"});
    cl.addOption({"element", "Element button to select (screenshot)", "id"});
    cl.addOption({"unit", "Unit to show in the chart panel (screenshot)", "id"});
    cl.addOption({"gage", "Gage to show in the chart panel (screenshot)", "id"});
    cl.addOption({"intervals", "Check the 95% intervals box (screenshot)"});
    cl.addOption({"test-alert-dialog", "Open the first flood alert's popup, render it to this PNG and exit", "file"});
    cl.addOption({"alert-member", "Member tab to select in the alert popup (test), e.g. p90", "member"});
    cl.addHelpOption();
    cl.process(app);
    if (cl.isSet("base")) base = cl.value("base");
    if (base.isEmpty()) base = QDir(QApplication::applicationDirPath()).filePath("../sample");

    DataSource data(base);
    MainWindow w(&data);
    w.show();
    const QString shot = cl.value("screenshot");
    const QString alertShot = cl.value("test-alert-dialog");
    w.load([&]() {
        if (!alertShot.isEmpty())
        {
            // wait for the alert files, open the popup, wait for its map images, then render both windows
            auto *poll = new QTimer(&app);
            auto tries = std::make_shared<int>(0);
            QObject::connect(poll, &QTimer::timeout, &app, [&, poll, tries]() {
                ++*tries;
                if (!w.alertDialog())
                {
                    if (w.alertSites().isEmpty() && *tries < 50) return;
                    if (w.alertSites().isEmpty()) { qWarning("no active flood alert"); app.exit(1); return; }
                    w.openAlert(w.alertSites().first());
                    if (cl.isSet("alert-member")) w.alertDialog()->selectMember(cl.value("alert-member"));
                    return;
                }
                if (w.alertDialog()->pendingImages() > 0 && *tries < 100) return;
                poll->stop();
                QTimer::singleShot(800, &app, [&]() {
                    w.alertDialog()->grab().save(alertShot);
                    if (!shot.isEmpty()) w.grab().save(shot);
                    app.quit();
                });
            });
            poll->start(100);
            return;
        }
        if (shot.isEmpty()) return;
        if (cl.isSet("element")) w.selectElement(cl.value("element"));
        if (cl.isSet("unit")) w.showUnit(cl.value("unit"), TimeMode::Both, MainWindow::ChartScope::Element);
        if (cl.isSet("intervals"))
            for (QCheckBox *b : w.findChildren<QCheckBox *>()) b->setChecked(true);
        if (cl.isSet("gage")) w.showGage(cl.value("gage"));
        QTimer::singleShot(1500, &app, [&]() {
            w.grab().save(shot);
            app.quit();
        });
    });
    return app.exec();
}
