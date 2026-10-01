// FinPulse Terminal — 双向管道子进程
//
// 头文件刻意不引入 <unistd.h> / <windows.h>：句柄一律用 std::intptr_t 装。
// 这样做的好处是任何包含了本头文件的翻译单元都不会被平台头污染
// （Windows 上 <windows.h> 会带来 min/max 宏、slots 之类一堆麻烦）。
//
// 线程模型：本类只提供阻塞 IO 原语，不含任何线程。
// 读线程、超时检查、重启策略都在上层的 RpcClient / PyEngine 里，
// 这样每个类只解决一个问题，也方便单独测试。
#pragma once

#include <cstddef>
#include <cstdint>
#include <mutex>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace fp {

class Subprocess {
public:
    struct Options {
        std::string              program;      ///< 可执行文件（不含参数；允许走 PATH 查找）
        std::vector<std::string> args;         ///< 参数列表（不含 argv[0]）
        std::string              working_dir;  ///< 空表示继承父进程
        /// 追加/覆盖的环境变量。子进程继承父进程环境后再应用这些。
        std::vector<std::pair<std::string, std::string>> env;
    };

    Subprocess() = default;
    ~Subprocess();

    Subprocess(const Subprocess&)            = delete;
    Subprocess& operator=(const Subprocess&) = delete;

    /// 启动。失败抛 std::runtime_error（消息里带 OS 错误码）。
    void start(const Options& opt);

    bool running() const noexcept { return running_; }

    /// 往子进程 stdin 写。子进程已退出（EPIPE）时抛异常并置 running_ = false。
    void write(std::string_view data);

    /// 从子进程 stdout 读一块。返回 0 表示 EOF。出错抛异常。
    std::size_t read(char* out, std::size_t cap);

    /// 等待 stdout 可读。超时返回 false。用它把"阻塞读"变成可轮询的，
    /// 上层就能在读循环里顺便做超时检查，不必再起一个看门狗线程。
    bool wait_readable(int timeout_ms);

    /// 关闭 stdin，向子进程发 EOF（正常情况下引擎会自行退出）。
    void close_stdin();

    /// 等待退出。timeout_ms < 0 表示无限等待。
    /// 返回 exit code；超时返回 -1。
    ///
    /// 可被多个线程并发调用：内部串行化，且已经收到过的退出码会被缓存复用。
    /// 这不是"以防万一" —— 读线程在读到 EOF 时会顺手回收子进程，
    /// 此时 stop() 再调 waitpid 会拿到 ECHILD，看起来像"超时"，
    /// 进而误报"引擎没退出"并去做一次多余的强杀。这个 bug 真实发生过。
    int wait(int timeout_ms = -1);

    /// 先 SIGTERM/CloseMainWindow，宽限期后 SIGKILL/TerminateProcess。
    void kill();

    long long   pid() const noexcept { return pid_; }
    std::string describe() const;

private:
    void cleanup() noexcept;

    std::intptr_t in_fd_{-1};   ///< 父 → 子（我们写）
    std::intptr_t out_fd_{-1};  ///< 子 → 父（我们读）
    long long     pid_{0};
    bool          running_{false};
    bool          stdin_open_{false};

    std::mutex wait_mu_;        ///< 保护 waitpid：子进程只能被回收一次
    int        cached_exit_{-1};
    bool       reaped_{false};
#if defined(_WIN32)
    std::intptr_t process_handle_{0};
#endif
};

}  // namespace fp
