// FinPulse Terminal — 图形界面入口
//
// 这里做的事很少，但每一件都有理由：
//
//   1. 高 DPI 缩放必须在 QApplication 构造之前设置。Qt6 默认已经开了缩放，
//      这里显式设一遍是为了同时打开"非整数倍缩放"（125% / 150% 这种笔记本
//      常见档位）。不设的话 Windows 上窗口会糊，一个字一个字地糊。
//   2. 等宽字体交给字体数据库去找，找不到就用系统默认。图表里的数字列
//      如果不等宽，小数点对不齐，看起来就像随手拼的界面。
//   3. 主窗口对象放在栈上，进程退出时按 RAII 顺序析构。这比 new 出来再
//      delete 更安全——中途抛异常也不会漏。

#include <QApplication>
#include <QFont>
#include <QFontDatabase>
#include <QDialog>
#include <QComboBox>
#include <QLineEdit>
#include <QPushButton>
#include <QSurfaceFormat>
#include <QTabWidget>
#include <QTimer>
#include <QWidget>

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "core/Version.h"
#include "gui/MainWindow.h"
#include "gui/SettingsDialog.h"   // --settings 截图要按类型找这个对话框

int main(int argc, char** argv) {
    // ── 早期退出 ──
    // 在 QApplication 之前处理，这样在没有显示环境（CI / 无头服务器）时
    // 也能安全地打印版本，不会因为建不了平台插件而失败。
    for (int i = 1; i < argc; ++i) {
        if (std::strcmp(argv[i], "--version") == 0 || std::strcmp(argv[i], "-V") == 0) {
            std::printf("%s %s\n", fp::kAppName, fp::kVersion);
            return 0;
        }
        if (std::strcmp(argv[i], "--help") == 0 || std::strcmp(argv[i], "-h") == 0) {
            std::printf(
                "用法: finpulse-gui [选项]\n\n"
                "  -h, --help              显示本帮助\n"
                "  -V, --version           显示版本\n"
                "      --screenshot <路径>   渲染一帧存成 PNG 后自动退出（无头环境可用）\n"
                "      --replay <毫秒>        截图前先点一次「回放行情」，等指定毫秒后再截\n"
                "      --debate <毫秒>        截图前先切到「AI 研判」页并点一次研判，等指定毫秒后再截\n"
                "      --ask <文本>           截图前先在「AI 研判」页提问并发送，等指定毫秒后再截\n"
                "      --ask-wait <毫秒>      --ask 之后等多久（默认 3000）\n"
                "      --settings <毫秒>      截图前点开「设置…」对话框，等指定毫秒后截对话框\n\n"
                "启动后会拉起源码目录 python/ 下的分析引擎；\n"
                "若引擎起不来，界面照样打开，错误会显示在右侧「日志」页。\n");
            return 0;
        }
    }

    // --screenshot: 等首轮分析跑完后把窗口截下来存成 PNG，然后退出。
    // 配合 QT_QPA_PLATFORM=offscreen 就能在没有显示器的机器上出图，
    // 用于文档配图和 CI 的视觉回归。
    QString screenshot;
    int     replay_wait_ms = -1;
    int     debate_wait_ms = -1;
    QString ask_text;
    int     ask_wait_ms = 3000;
    int     settings_wait_ms = -1;
    QString settings_probe;   // "provider|api_key[|base_url]"
    for (int i = 1; i + 1 < argc; ++i) {
        if (std::strcmp(argv[i], "--screenshot") == 0) {
            screenshot = QString::fromLocal8Bit(argv[i + 1]);
        } else if (std::strcmp(argv[i], "--replay") == 0) {
            replay_wait_ms = std::atoi(argv[i + 1]);
        } else if (std::strcmp(argv[i], "--debate") == 0) {
            debate_wait_ms = std::atoi(argv[i + 1]);
        } else if (std::strcmp(argv[i], "--ask") == 0) {
            ask_text = QString::fromLocal8Bit(argv[i + 1]);
        } else if (std::strcmp(argv[i], "--ask-wait") == 0) {
            ask_wait_ms = std::atoi(argv[i + 1]);
        } else if (std::strcmp(argv[i], "--settings") == 0) {
            settings_wait_ms = std::atoi(argv[i + 1]);
        } else if (std::strcmp(argv[i], "--settings-probe") == 0) {
            settings_probe = QString::fromLocal8Bit(argv[i + 1]);
        }
    }

    // 切到「AI 研判」页。`--ask` / `--debate` 共用 —— 两处各写一遍，
    // 将来页签改了名字只会有一处被想起来跟着改。
    const auto focus_agent_tab = [](QWidget& root) {
        for (QTabWidget* tabs : root.findChildren<QTabWidget*>()) {
            for (int i = 0; i < tabs->count(); ++i) {
                if (tabs->tabText(i) == QStringLiteral("AI 研判")) {
                    tabs->setCurrentIndex(i);
                    return;
                }
            }
        }
    };

    // ── 高 DPI ──
    // AA_EnableHighDpiScaling 在 Qt6 里已经是默认行为，不必再设；
    // 但 PassThrough 策略要显式打开，否则 125% 缩放会被四舍五入成 100%，
    // 界面元素偏小。这两行的顺序不能调换——必须在 QApplication 之前。
    QApplication::setHighDpiScaleFactorRoundingPolicy(
        Qt::HighDpiScaleFactorRoundingPolicy::PassThrough);

    // OpenGL/Surface 格式：默认就好，这里只为把色彩深度固定成 32 位，
    // 免得某些远程桌面会话下拿到 16 位色导致渐变出现色带。
    QSurfaceFormat fmt;
    fmt.setDepthBufferSize(24);
    fmt.setSamples(0);
    QSurfaceFormat::setDefaultFormat(fmt);

    QApplication app(argc, argv);
    QApplication::setApplicationName(QString::fromUtf8(fp::kAppName));
    QApplication::setApplicationVersion(QString::fromUtf8(fp::kVersion));
    QApplication::setOrganizationName(QStringLiteral("FinPulse"));

    // ── 字体 ──
    // 候选按优先级排：Cascadia Mono 是 Windows Terminal 自带的新字体，
    // 其次是 Consolas（Windows）/ Menlo（macOS）/ DejaVu Sans Mono（Linux）。
    // 一个都找不到时用系统默认，功能不受影响，只是数字列可能不齐。
    const QStringList mono_candidates = {
        QStringLiteral("Cascadia Mono"),
        QStringLiteral("Consolas"),
        QStringLiteral("Menlo"),
        QStringLiteral("DejaVu Sans Mono"),
        QStringLiteral("Courier New"),
    };
    for (const QString& name : mono_candidates) {
        if (QFontDatabase::families().contains(name)) {
            QFont f(name);
            f.setStyleHint(QFont::Monospace);
            f.setPointSizeF(9.5);
            app.setFont(f);
            break;
        }
    }

    fp::gui::MainWindow window;
    window.show();

    if (!screenshot.isEmpty()) {
        // 首轮分析要走一次 RPC（几十到几百毫秒）+ 主线程取数画图，
        // 给 5 秒余量。宁可多等一会儿，也不要截到一张空白图。
        QTimer::singleShot(5000, &app, [&window, &app, screenshot, replay_wait_ms,
                                        debate_wait_ms, ask_text, ask_wait_ms,
                                        settings_wait_ms, settings_probe, focus_agent_tab] {
            int wait_ms = -1;

            if (replay_wait_ms >= 0) {
                // 用"找到按钮并点击"来触发回放，而不是给 MainWindow 开一个
                // 仅供测试调用的入口。走的是和用户点击完全相同的代码路径，
                // 这条路径有问题才会被抓到 —— 测试专用的后门测不出真问题。
                for (QPushButton* b : window.findChildren<QPushButton*>()) {
                    if (b->text() == QStringLiteral("回放行情")) {
                        b->click();
                        break;
                    }
                }
                wait_ms = replay_wait_ms;
            }

            if (debate_wait_ms >= 0) {
                // 同理：切页签 + 点按钮，走的是用户路径。
                focus_agent_tab(window);
                for (QPushButton* b : window.findChildren<QPushButton*>()) {
                    if (b->text() == QStringLiteral("跑投委会研判")) {
                        b->click();
                        break;
                    }
                }
                wait_ms = debate_wait_ms;
            }

            if (!ask_text.isEmpty()) {
                // 一次**真实对话**：切页签 → 往输入框打字 → 点「发送」。
                //
                // 为什么不直接调 sendChat()：这条链路上真正容易坏的是
                // "控件连没连上"、"回车/点击有没有接到同一个槽"、
                // "回复有没有被渲染出来" —— 直接调函数把这三点全绕过去了。
                // 它同时也是唯一能给"这一页看起来对不对"出证据的办法：
                // 单测全绿只说明字符串拼对了，不说明屏幕上出现了气泡。
                focus_agent_tab(window);
                if (auto* input = window.findChild<QLineEdit*>(QStringLiteral("chatInput"))) {
                    input->setText(ask_text);
                }
                if (auto* send = window.findChild<QPushButton*>(QStringLiteral("chatSend"))) {
                    send->click();
                }
                wait_ms = ask_wait_ms;
            }

            if (settings_wait_ms >= 0) {
                // 「设置…」打开的对话框是**模态**的：exec() 会一直阻塞到它关闭。
                // 所以所有定时器都必须在点按钮**之前**安排好 —— 写在点击后面
                // 的代码，要等对话框关掉之后才轮得到执行。
                //
                // settings_probe 形如 "provider|api_key[|base_url]"。填的全是
                // 用户路径能填的格子：服务商按 findData 精确匹配（数据值，
                // 不是显示文本 —— 文案一改钩子就悄悄失效的那种坑）、粘贴
                // 密钥、可选端点，然后点「测试并获取模型」。走的是和真人
                // 点击完全相同的信号链。
                const QStringList probe_parts = settings_probe.isEmpty()
                    ? QStringList()
                    : settings_probe.split(QLatin1Char('|'));

                QTimer::singleShot(800, &app, [&app, probe_parts] {
                    fp::gui::SettingsDialog* dlg = nullptr;
                    for (QWidget* w : QApplication::topLevelWidgets()) {
                        if (!w->isVisible()) continue;
                        if (auto* d = qobject_cast<fp::gui::SettingsDialog*>(w)) {
                            dlg = d;
                            break;
                        }
                    }
                    if (!dlg) return;   // 对话框还没起来（或已被关掉）

                    if (!probe_parts.isEmpty()) {
                        if (auto* combo = dlg->findChild<QComboBox*>(
                                QStringLiteral("settingsProvider"))) {
                            const int idx = combo->findData(probe_parts.at(0));
                            if (idx >= 0) combo->setCurrentIndex(idx);
                        }
                    }
                    if (probe_parts.size() >= 2) {
                        if (auto* key = dlg->findChild<QLineEdit*>(
                                QStringLiteral("settingsKey"))) {
                            key->setText(probe_parts.at(1));
                        }
                    }
                    if (probe_parts.size() >= 3) {
                        if (auto* url = dlg->findChild<QLineEdit*>(
                                QStringLiteral("settingsBaseUrl"))) {
                            url->setText(probe_parts.at(2));
                        }
                    }
                    if (probe_parts.isEmpty()) return;   // 只开对话框不探测

                    if (auto* probe = dlg->findChild<QPushButton*>(
                            QStringLiteral("settingsProbe"))) {
                        probe->click();
                    }
                });

                QTimer::singleShot(std::max(0, settings_wait_ms), &app, [&app, screenshot, settings_wait_ms] {
                    fp::gui::SettingsDialog* dlg = nullptr;
                    for (QWidget* w : QApplication::topLevelWidgets()) {
                        if (!w->isVisible()) continue;
                        if (auto* d = qobject_cast<fp::gui::SettingsDialog*>(w)) {
                            dlg = d;
                            break;
                        }
                    }
                    if (!dlg) {
                        std::fprintf(stderr, "没有找到可见的设置对话框\n");
                        QCoreApplication::quit();
                        return;
                    }
                    const bool ok = dlg->grab().save(screenshot);
                    std::fprintf(ok ? stdout : stderr, "%s: %s\n",
                                 ok ? "截图已保存" : "截图保存失败",
                                 screenshot.toLocal8Bit().constData());
                    dlg->reject();          // 关掉它，让下面的 exec() 返回
                    QCoreApplication::quit();
                });

                // 点「设置…」—— 走的是和用户点击完全相同的代码路径。
                // 这一句会一直阻塞到对话框关闭（模态 exec）。
                for (QPushButton* b : window.findChildren<QPushButton*>()) {
                    if (b->text() == QStringLiteral("设置…")) {
                        b->click();
                        break;
                    }
                }
                return;   // 截图已经安排好了，不要再走下面那条通用路径
            }

            QTimer::singleShot(std::max(0, wait_ms), &app, [&window, screenshot] {
                const bool ok = window.grab().save(screenshot);
                std::fprintf(ok ? stdout : stderr, "%s: %s\n",
                             ok ? "截图已保存" : "截图保存失败",
                             screenshot.toLocal8Bit().constData());
                QCoreApplication::quit();
            });
        });
    }

    return app.exec();
}
