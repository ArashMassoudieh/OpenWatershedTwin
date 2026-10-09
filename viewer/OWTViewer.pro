# OpenWatershedTwin map viewer (Qt Widgets; desktop or WebAssembly).
#   desktop:      qmake OWTViewer.pro && make
#   WebAssembly:  source ~/emsdk/emsdk_env.sh; ~/Qt/6.8.2/wasm_singlethread/bin/qmake OWTViewer.pro && make
#                 -> index.html (our page, from viewer/index.html) + OWTViewer.js/.wasm + qtloader.js,
#                    served next to viewer_config.json, gis/ and outputs/ (OWTViewer.html is Qt's default page)
QT += widgets network charts
CONFIG += c++17
TARGET = OWTViewer
SOURCES += main.cpp DataSource.cpp MapView.cpp ChartPanel.cpp MainWindow.cpp
HEADERS += DataSource.h MapView.h ChartPanel.h MainWindow.h
wasm {
    QMAKE_LFLAGS += -sINITIAL_MEMORY=134217728
    # our page shell (watershed splash) next to OWTViewer.js/.wasm; serve it as index.html instead of OWTViewer.html
    QMAKE_POST_LINK += $$QMAKE_COPY $$shell_path($$PWD/index.html) $$shell_path($$OUT_PWD/index.html)
}
