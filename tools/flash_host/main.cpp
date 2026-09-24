#include <QApplication>
#include <QCloseEvent>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonObject>
#include <QNetworkAccessManager>
#include <QNetworkRequest>
#include <QTimer>
#include <QUrl>
#include <QWebEngineProfile>
#include <QWebEngineScript>
#include <QWebEngineScriptCollection>
#include <QWebEngineSettings>
#include <QWebEngineView>
#include <QWebEnginePage>
#include <QByteArray>
#include <cstdio>

#ifdef Q_OS_WIN
#include <windows.h>
#endif

static QString exeDir()
{
#ifdef Q_OS_WIN
    wchar_t buf[MAX_PATH] = {0};
    if (GetModuleFileNameW(nullptr, buf, MAX_PATH) > 0)
        return QFileInfo(QString::fromWCharArray(buf)).absolutePath();
#endif
    return QCoreApplication::applicationDirPath();
}

static QByteArray readAll(const QString& path)
{
    QFile f(path);
    if (!f.open(QIODevice::ReadOnly)) return {};
    return f.readAll();
}

class PktPage : public QWebEnginePage
{
public:
    QUrl pktUrl;
    QNetworkAccessManager* nam = nullptr;
    using QWebEnginePage::QWebEnginePage;
protected:
    void javaScriptAlert(const QUrl& url, const QString& msg) override
    {
        fprintf(stderr, "js-alert: %s\n", qUtf8Printable(msg));
        QWebEnginePage::javaScriptAlert(url, msg);
    }
    void javaScriptConsoleMessage(JavaScriptConsoleMessageLevel,
                                  const QString& message, int,
                                  const QString&) override
    {
        if (!message.startsWith(QLatin1String("[hjdz-pkt]"))) {
            fprintf(stderr, "js: %s\n", qUtf8Printable(message.left(400)));
            return;
        }
        const QStringList f = message.mid(10).split(QChar(0x0001));
        if (f.size() < 3 || pktUrl.isEmpty() || !nam)
            return;
        QJsonObject o;
        o.insert(QStringLiteral("dir"), f[0]);
        o.insert(QStringLiteral("cls"), f[1]);
        o.insert(QStringLiteral("dump"), f[2]);
        o.insert(QStringLiteral("raw"), f.size() >= 4 ? f[3] : QString());
        QNetworkRequest req(pktUrl);
        req.setHeader(QNetworkRequest::ContentTypeHeader,
                      QStringLiteral("application/json"));
        nam->post(req, QJsonDocument(o).toJson(QJsonDocument::Compact));
    }
};

class HostView : public QWebEngineView
{
protected:
    void closeEvent(QCloseEvent* e) override
    {
        fprintf(stderr, "window-close\n");
        QWebEngineView::closeEvent(e);
    }
};

int main(int argc, char* argv[])
{
    QString cfgPath;
    for (int i = 1; i < argc; ++i) {
        if (QByteArray(argv[i]) == "--cfg" && i + 1 < argc)
            cfgPath = QString::fromLocal8Bit(argv[++i]);
    }
    if (cfgPath.isEmpty()) {
        fprintf(stderr, "usage: flash_host --cfg flash_host.json\n");
        return 2;
    }
    const QJsonObject cfg = QJsonDocument::fromJson(readAll(cfgPath)).object();
    const QString url = cfg.value(QStringLiteral("url")).toString();
    const QString cdn = cfg.value(QStringLiteral("cdn_host")).toString(
        QStringLiteral("redwar-cdn.sincetimes.com"));
    const int proxyPort = cfg.value(QStringLiteral("proxy_port")).toInt(8443);
    const QString spki = cfg.value(QStringLiteral("spki")).toString();
    QString flash = cfg.value(QStringLiteral("flash")).toString();
    const QString pkt = cfg.value(QStringLiteral("pkt_url")).toString();
    if (url.isEmpty() || spki.isEmpty()) {
        fprintf(stderr, "flash_host.json needs url and spki\n");
        return 2;
    }
    if (flash.isEmpty()) {
        const QString cand = exeDir() + QStringLiteral("/pepflashplayer.dll");
        if (QFile::exists(cand)) flash = cand;
    }
    if (!QFile::exists(flash)) {
        fprintf(stderr, "pepflashplayer.dll not found: %s\n",
                qUtf8Printable(flash));
        return 2;
    }

    const QString pac = QStringLiteral(
        "function FindProxyForURL(url,host){"
        "if(host==\"%1\")return \"PROXY 127.0.0.1:%2\";"
        "return \"DIRECT\";}").arg(cdn).arg(proxyPort);
    QByteArray flags = "--disable-logging --log-level=3 --allow-outdated-plugins "
                       "--enable-plugins --allow-running-insecure-content";
    flags += " --ppapi-flash-path=\"" + QDir::toNativeSeparators(flash).toUtf8() + "\"";
    flags += " --ppapi-flash-version=34.0.0.330";
    flags += " --proxy-pac-url=data:application/x-ns-proxy-autoconfig;base64,";
    flags += pac.toUtf8().toBase64();
    flags += " --ignore-certificate-errors-spki-list=" + spki.toUtf8();
    qputenv("QTWEBENGINE_CHROMIUM_FLAGS", flags);
    qputenv("QT_LOGGING_RULES", "qt.webengine.*=false");

    QCoreApplication::setAttribute(Qt::AA_EnableHighDpiScaling);
    QApplication app(argc, argv);
    app.setApplicationName(QStringLiteral("tankstorm-flash-host"));

    auto enableFlash = [](QWebEngineSettings* s) {
        s->setAttribute(QWebEngineSettings::PluginsEnabled, true);
        s->setAttribute(QWebEngineSettings::JavascriptEnabled, true);
        s->setAttribute(QWebEngineSettings::JavascriptCanOpenWindows, true);
        s->setAttribute(QWebEngineSettings::LocalContentCanAccessRemoteUrls, true);
        s->setAttribute(QWebEngineSettings::AllowRunningInsecureContent, true);
    };
    enableFlash(QWebEngineSettings::defaultSettings());

    auto* profile = new QWebEngineProfile(QString(), &app);
    profile->setHttpCacheType(QWebEngineProfile::MemoryHttpCache);
    profile->setPersistentCookiesPolicy(QWebEngineProfile::NoPersistentCookies);
    enableFlash(profile->settings());

    QWebEngineScript hook;
    hook.setName(QStringLiteral("pkt_hook"));
    hook.setInjectionPoint(QWebEngineScript::DocumentCreation);
    hook.setWorldId(QWebEngineScript::MainWorld);
    hook.setRunsOnSubFrames(true);
    hook.setSourceCode(QStringLiteral(R"JS(
(function(){
  if (window.__pkt) return;
  window.__pktOn = function(){ return true; };
  window.__pkt = function(dir, cls, dump, raw){
    try {
      console.log('[hjdz-pkt]' + dir + '\u0001' + cls + '\u0001' + dump
                  + '\u0001' + (raw || ''));
    } catch(e) {}
    return null;
  };
})();
)JS"));
    profile->scripts()->insert(hook);

    auto* nam = new QNetworkAccessManager(&app);
    HostView view;
    auto* page = new PktPage(profile, &view);
    page->nam = nam;
    page->pktUrl = QUrl(pkt);
    view.setPage(page);
    enableFlash(page->settings());
    QObject::disconnect(page, &QWebEnginePage::windowCloseRequested, &view, nullptr);
    QObject::connect(page, &QWebEnginePage::windowCloseRequested, []() {
        fprintf(stderr, "js-window.close ignored\n");
    });
    QObject::connect(page, &QWebEnginePage::renderProcessTerminated, &view,
                     [&view](QWebEnginePage::RenderProcessTerminationStatus st, int code) {
        fprintf(stderr, "renderer-exit status=%d code=%d\n", int(st), code);
        QTimer::singleShot(400, &view, [&view]() { view.reload(); });
    });
    QObject::connect(page, &QWebEnginePage::loadFinished, [](bool ok) {
        fprintf(stderr, "load-%s\n", ok ? "ok" : "fail");
    });
    view.resize(1280, 800);
    view.setWindowTitle(QStringLiteral("坦克风暴 · 抓包"));
    view.load(QUrl(url));
    view.show();
    return app.exec();
}
