# OpenWatershedTwin map viewer (Qt Widgets; desktop or WebAssembly).
#   desktop:      qmake OWTViewer.pro && make
#   WebAssembly:  source ~/emsdk/emsdk_env.sh; ~/Qt/6.8.2/wasm_singlethread/bin/qmake OWTViewer.pro && make
#                 -> OWTViewer.html/.js/.wasm, served next to viewer_config.json, gis/ and outputs/
QT += widgets network charts
CONFIG += c++17
TARGET = OWTViewer
SOURCES += main.cpp DataSource.cpp MapView.cpp ChartPanel.cpp MainWindow.cpp
HEADERS += DataSource.h MapView.h ChartPanel.h MainWindow.h
wasm {
    QMAKE_LFLAGS += -sINITIAL_MEMORY=134217728
}
