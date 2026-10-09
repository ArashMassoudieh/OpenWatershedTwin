// OpenWatershedTwin viewer: entry point.
//   browser (WebAssembly): data are fetched relative to the page URL (the twin's web folder)
//   desktop: OWTViewer [--base <folder or URL>] [--screenshot out.png [--element Soil_2] [--unit SC30]]
#include "DataSource.h"
#include "MainWindow.h"
#include "MapView.h"

#include <QApplication>
#include <QCommandLineParser>
#include <QDir>
#include <QTimer>

#ifdef Q_OS_WASM
#include <emscripten/val.h>
#endif

int main(int argc, char *argv[])
{
    QApplication app(argc, argv);
    QApplication::setApplicationName("OWTViewer");

    QString base;
#ifdef Q_OS_WASM
    base = QString::fromStdString(emscripten::val::global("location")["href"].as<std::string>());
    base = base.left(base.lastIndexOf('/') + 1);
#endif
    QCommandLineParser cl;
    cl.addOption({"base", "Folder or URL with viewer_config.json", "base"});
    cl.addOption({"screenshot", "Render the window to this PNG and exit", "file"});
    cl.addOption({"element", "Element button to select (screenshot)", "id"});
    cl.addOption({"unit", "Unit to show in the chart panel (screenshot)", "id"});
    cl.addOption({"gage", "Gage to show in the chart panel (screenshot)", "id"});
    cl.addHelpOption();
    cl.process(app);
    if (cl.isSet("base")) base = cl.value("base");
    if (base.isEmpty()) base = QDir(QApplication::applicationDirPath()).filePath("../sample");

    DataSource data(base);
    MainWindow w(&data);
    w.show();
    const QString shot = cl.value("screenshot");
    w.load([&]() {
        if (shot.isEmpty()) return;
        if (cl.isSet("element")) w.selectElement(cl.value("element"));
        if (cl.isSet("unit")) w.showUnit(cl.value("unit"), TimeMode::Both, MainWindow::ChartScope::Element);
        if (cl.isSet("gage")) w.showGage(cl.value("gage"));
        QTimer::singleShot(1500, &app, [&]() {
            w.grab().save(shot);
            app.quit();
        });
    });
    return app.exec();
}
