QT += core gui widgets webenginewidgets network
CONFIG += c++17
TARGET = flash_host
TEMPLATE = app
QMAKE_CXXFLAGS += /utf-8
SOURCES += main.cpp

win32 {
    CONFIG(debug, debug|release) {
        BIN_DIR = $$OUT_PWD/debug
    } else {
        BIN_DIR = $$OUT_PWD/release
    }
    FLASH_SRC = $$(HJDZ_FLASH)
    isEmpty(FLASH_SRC) {
        FLASH_SRC = $$PWD/../../../hjdz-automation/pepflashplayer.dll
    }
    FLASH_DST = $$shell_path($$BIN_DIR/pepflashplayer.dll)
    FLASH_SRC_WIN = $$shell_path($$FLASH_SRC)
    QMAKE_POST_LINK += $$escape_expand(\\n\\t) \
        if exist $$FLASH_SRC_WIN copy /Y $$FLASH_SRC_WIN $$FLASH_DST
}
