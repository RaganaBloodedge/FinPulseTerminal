#include "bridge/Subprocess.h"

#include <chrono>
#include <cstring>
#include <stdexcept>
#include <thread>

#if defined(_WIN32)
#  define WIN32_LEAN_AND_MEAN
#  include <windows.h>
#  include <map>
#else
#  include <cerrno>
#  include <csignal>
#  include <cstdlib>
#  include <fcntl.h>
#  include <poll.h>
#  include <sys/wait.h>
#  include <unistd.h>
#endif

namespace fp {

namespace {

std::string os_error_text(int code) {
#if defined(_WIN32)
    return "Win32 错误码 " + std::to_string(code);
#else
    return std::strerror(code);
#endif
}

}  // namespace

#if !defined(_WIN32)
// ══════════════════════════════════════════════════════════
//  POSIX 实现（Linux / macOS）
// ══════════════════════════════════════════════════════════

namespace {

void close_fd(std::intptr_t& fd) {
    if (fd >= 0) {
        ::close(static_cast<int>(fd));
        fd = -1;
    }
}

}  // namespace

void Subprocess::start(const Options& opt) {
    if (running_) throw std::runtime_error("[proc] 进程已在运行");
    if (opt.program.empty()) throw std::invalid_argument("[proc] program 不能为空");

    // 往已关闭的管道写数据会触发 SIGPIPE，默认动作是直接杀掉整个终端进程。
    // 我们需要的是 write() 返回 EPIPE —— 那正是"引擎挂了"的可靠信号。
    // 屏蔽动作是进程级且幂等的，这里做一次即可。
    static bool sigpipe_done = false;
    if (!sigpipe_done) {
        ::signal(SIGPIPE, SIG_IGN);
        sigpipe_done = true;
    }

    int to_child[2]{-1, -1};
    int from_child[2]{-1, -1};
    if (::pipe(to_child) != 0) {
        throw std::runtime_error("[proc] 创建管道失败: " + os_error_text(errno));
    }
    if (::pipe(from_child) != 0) {
        const int e = errno;
        ::close(to_child[0]);
        ::close(to_child[1]);
        throw std::runtime_error("[proc] 创建管道失败: " + os_error_text(e));
    }

    const pid_t pid = ::fork();
    if (pid < 0) {
        const int e = errno;
        ::close(to_child[0]);  ::close(to_child[1]);
        ::close(from_child[0]); ::close(from_child[1]);
        throw std::runtime_error("[proc] fork 失败: " + os_error_text(e));
    }

    if (pid == 0) {
        // ── 子进程 ──
        // fork 之后只能调 async-signal-safe 的函数。dup2/close/chdir/execvp 安全；
        // setenv 严格来说会碰 malloc 锁，父进程若是多线程理论上可能卡死。
        // 现实是启动阶段父进程基本单线程，这里接受这个风险并留了记号。
        // TODO: 改成在父进程构造 envp 数组 + execve，彻底规避。
        ::dup2(to_child[0], STDIN_FILENO);
        ::dup2(from_child[1], STDOUT_FILENO);
        // stderr 不重定向 —— 引擎日志直接透传到终端，方便排查
        ::close(to_child[0]);  ::close(to_child[1]);
        ::close(from_child[0]); ::close(from_child[1]);

        if (!opt.working_dir.empty() && ::chdir(opt.working_dir.c_str()) != 0) {
            ::_exit(126);
        }
        for (const auto& kv : opt.env) {
            ::setenv(kv.first.c_str(), kv.second.c_str(), 1);
        }

        std::vector<char*> argv;
        argv.reserve(opt.args.size() + 2);
        argv.push_back(const_cast<char*>(opt.program.c_str()));
        for (const auto& a : opt.args) argv.push_back(const_cast<char*>(a.c_str()));
        argv.push_back(nullptr);

        ::execvp(opt.program.c_str(), argv.data());
        ::_exit(127);  // exec 失败才走到这
    }

    // ── 父进程 ──
    ::close(to_child[0]);
    ::close(from_child[1]);

    in_fd_      = to_child[1];
    out_fd_     = from_child[0];
    pid_        = static_cast<long long>(pid);
    running_    = true;
    stdin_open_ = true;
    cached_exit_ = -1;
    reaped_      = false;
}

void Subprocess::write(std::string_view data) {
    if (!running_ || in_fd_ < 0) throw std::runtime_error("[proc] 管道不可写");

    const char* p    = data.data();
    std::size_t left = data.size();
    while (left > 0) {
        const ssize_t n = ::write(static_cast<int>(in_fd_), p, left);
        if (n < 0) {
            if (errno == EINTR) continue;
            if (errno == EPIPE) {
                running_    = false;
                stdin_open_ = false;
                throw std::runtime_error("[proc] 子进程已关闭输入管道");
            }
            throw std::runtime_error("[proc] 写入失败: " + os_error_text(errno));
        }
        p    += n;
        left -= static_cast<std::size_t>(n);
    }
}

std::size_t Subprocess::read(char* out, std::size_t cap) {
    for (;;) {
        const ssize_t n = ::read(static_cast<int>(out_fd_), out, cap);
        if (n >= 0) return static_cast<std::size_t>(n);
        if (errno == EINTR) continue;
        throw std::runtime_error("[proc] 读取失败: " + os_error_text(errno));
    }
}

bool Subprocess::wait_readable(int timeout_ms) {
    if (out_fd_ < 0) return false;

    struct pollfd pfd{};
    pfd.fd     = static_cast<int>(out_fd_);
    pfd.events = POLLIN;

    for (;;) {
        const int rc = ::poll(&pfd, 1, timeout_ms);
        if (rc > 0) {
            // POLLHUP 也要返回 true —— 让上层立刻去 read()，拿到 EOF 才知道对面没了
            return (pfd.revents & (POLLIN | POLLHUP | POLLERR)) != 0;
        }
        if (rc == 0) return false;  // 超时
        if (errno == EINTR) continue;
        return false;
    }
}

void Subprocess::close_stdin() {
    close_fd(in_fd_);
    stdin_open_ = false;
}

int Subprocess::wait(int timeout_ms) {
    // 子进程只能被回收一次。读线程读到 EOF 时会顺手收一次，
    // 之后 stop() 再来问就该拿到缓存值，而不是 ECHILD 被误读成"还在跑"。
    std::lock_guard<std::mutex> lk(wait_mu_);
    if (reaped_) return cached_exit_;
    if (pid_ <= 0) return -1;

    const auto reap = [this](int status) -> int {
        cached_exit_ = WIFEXITED(status) ? WEXITSTATUS(status) : -1;
        reaped_      = true;
        pid_         = 0;
        running_     = false;
        return cached_exit_;
    };

    if (timeout_ms < 0) {
        int status = 0;
        while (::waitpid(static_cast<pid_t>(pid_), &status, 0) < 0) {
            if (errno != EINTR) return -1;
        }
        return reap(status);
    }

    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
    for (;;) {
        int status = 0;
        const pid_t rc = ::waitpid(static_cast<pid_t>(pid_), &status, WNOHANG);
        if (rc == static_cast<pid_t>(pid_)) return reap(status);
        if (rc < 0 && errno != EINTR) return -1;
        if (std::chrono::steady_clock::now() >= deadline) return -1;  // 超时，进程还活着
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
}

void Subprocess::kill() {
    if (pid_ <= 0) return;
    ::kill(static_cast<pid_t>(pid_), SIGTERM);
    if (wait(1500) >= 0) return;         // 给 1.5s 优雅退出窗口
    ::kill(static_cast<pid_t>(pid_), SIGKILL);
    wait(1000);
}

void Subprocess::cleanup() noexcept {
    close_fd(in_fd_);
    close_fd(out_fd_);
    if (pid_ > 0 && running_) {
        ::kill(static_cast<pid_t>(pid_), SIGKILL);
        int status = 0;
        ::waitpid(static_cast<pid_t>(pid_), &status, 0);
        pid_ = 0;
    } else if (pid_ > 0) {
        int status = 0;
        ::waitpid(static_cast<pid_t>(pid_), &status, WNOHANG);  // 回收僵尸，拿不到就算了
        pid_ = 0;
    }
    running_ = false;
}

std::string Subprocess::describe() const {
    return "pid=" + std::to_string(pid_);
}

Subprocess::~Subprocess() { cleanup(); }

#else
// ══════════════════════════════════════════════════════════
//  Windows 实现
// ══════════════════════════════════════════════════════════

namespace {

std::wstring to_wide(const std::string& s) {
    if (s.empty()) return {};
    const int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), static_cast<int>(s.size()), nullptr, 0);
    if (n <= 0) return {};
    std::wstring w(static_cast<std::size_t>(n), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), static_cast<int>(s.size()), w.data(), n);
    return w;
}

/// 按 Windows 命令行解析规则给参数加引号。
/// 反斜杠只在"紧接着引号"时才需要翻倍，这是 CreateProcess 解析器的经典坑。
std::wstring quote_arg(const std::wstring& a) {
    if (!a.empty() && a.find_first_of(L" \t\n\v\"") == std::wstring::npos) return a;

    std::wstring out = L"\"";
    for (auto it = a.begin();; ++it) {
        unsigned backslashes = 0;
        while (it != a.end() && *it == L'\\') { ++it; ++backslashes; }
        if (it == a.end()) {
            out.append(backslashes * 2, L'\\');
            break;
        }
        if (*it == L'"') {
            out.append(backslashes * 2 + 1, L'\\');
        } else {
            out.append(backslashes, L'\\');
        }
        out.push_back(*it);
    }
    out.push_back(L'"');
    return out;
}

std::vector<wchar_t> build_environment_block(
    const std::vector<std::pair<std::string, std::string>>& overrides) {
    if (overrides.empty()) return {};  // 空 → CreateProcess 传 nullptr，直接继承

    std::map<std::wstring, std::wstring> merged;
    if (LPWCH cur = GetEnvironmentStringsW()) {
        for (LPWCH p = cur; *p;) {
            std::wstring entry = p;
            p += entry.size() + 1;
            if (entry.empty() || entry[0] == L'=') continue;  // 跳过 "=C:" 这类特殊项
            const auto eq = entry.find(L'=');
            if (eq != std::wstring::npos) merged[entry.substr(0, eq)] = entry.substr(eq + 1);
        }
        FreeEnvironmentStringsW(cur);
    }
    for (const auto& kv : overrides) merged[to_wide(kv.first)] = to_wide(kv.second);

    std::vector<wchar_t> block;
    for (const auto& kv : merged) {
        block.insert(block.end(), kv.first.begin(), kv.first.end());
        block.push_back(L'=');
        block.insert(block.end(), kv.second.begin(), kv.second.end());
        block.push_back(L'\0');
    }
    block.push_back(L'\0');  // 环境块以双 NUL 结尾
    return block;
}

void close_handle(std::intptr_t& h) {
    if (h) {
        CloseHandle(reinterpret_cast<HANDLE>(h));
        h = 0;
    }
}

}  // namespace

void Subprocess::start(const Options& opt) {
    if (running_) throw std::runtime_error("[proc] 进程已在运行");
    if (opt.program.empty()) throw std::invalid_argument("[proc] program 不能为空");

    SECURITY_ATTRIBUTES sa{};
    sa.nLength        = sizeof(sa);
    sa.bInheritHandle = TRUE;

    HANDLE c_in = nullptr, c_in_w = nullptr;    // 子进程 stdin 的读端 / 我们的写端
    HANDLE c_out_r = nullptr, c_out = nullptr;  // 我们的读端 / 子进程 stdout 的写端

    if (!CreatePipe(&c_in, &c_in_w, &sa, 0)) {
        throw std::runtime_error("[proc] CreatePipe 失败: " + os_error_text(static_cast<int>(GetLastError())));
    }
    if (!CreatePipe(&c_out_r, &c_out, &sa, 0)) {
        const DWORD e = GetLastError();
        CloseHandle(c_in); CloseHandle(c_in_w);
        throw std::runtime_error("[proc] CreatePipe 失败: " + os_error_text(static_cast<int>(e)));
    }
    // 我们这一端不能被继承，否则子进程退出后管道不会 EOF
    SetHandleInformation(c_in_w, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(c_out_r, HANDLE_FLAG_INHERIT, 0);

    std::wstring cmd = quote_arg(to_wide(opt.program));
    for (const auto& a : opt.args) {
        cmd.push_back(L' ');
        cmd += quote_arg(to_wide(a));
    }
    std::vector<wchar_t> cmd_buf(cmd.begin(), cmd.end());
    cmd_buf.push_back(L'\0');

    std::vector<wchar_t> env_block = build_environment_block(opt.env);
    const std::wstring   cwd       = to_wide(opt.working_dir);

    STARTUPINFOW si{};
    si.cb          = sizeof(si);
    si.dwFlags     = STARTF_USESTDHANDLES;
    si.hStdInput   = c_in;
    si.hStdOutput  = c_out;
    si.hStdError   = GetStdHandle(STD_ERROR_HANDLE);

    PROCESS_INFORMATION pi{};
    const BOOL ok = CreateProcessW(
        nullptr, cmd_buf.data(),
        nullptr, nullptr, TRUE,
        CREATE_NO_WINDOW,
        env_block.empty() ? nullptr : env_block.data(),
        cwd.empty() ? nullptr : cwd.c_str(),
        &si, &pi);

    if (!ok) {
        const DWORD e = GetLastError();
        CloseHandle(c_in); CloseHandle(c_in_w); CloseHandle(c_out_r); CloseHandle(c_out);
        throw std::runtime_error("[proc] CreateProcess 失败: " + os_error_text(static_cast<int>(e)));
    }

    CloseHandle(c_in);   // 子进程侧句柄交出去后我们不再需要
    CloseHandle(c_out);
    CloseHandle(pi.hThread);

    in_fd_          = reinterpret_cast<std::intptr_t>(c_in_w);
    out_fd_         = reinterpret_cast<std::intptr_t>(c_out_r);
    process_handle_ = reinterpret_cast<std::intptr_t>(pi.hProcess);
    pid_            = static_cast<long long>(pi.dwProcessId);
    running_        = true;
    stdin_open_     = true;
    cached_exit_    = -1;
    reaped_         = false;
}

void Subprocess::write(std::string_view data) {
    if (!running_ || !in_fd_) throw std::runtime_error("[proc] 管道不可写");

    const char* p    = data.data();
    std::size_t left = data.size();
    while (left > 0) {
        DWORD written = 0;
        const BOOL ok = WriteFile(reinterpret_cast<HANDLE>(in_fd_), p,
                                  static_cast<DWORD>(left), &written, nullptr);
        if (!ok) {
            running_    = false;
            stdin_open_ = false;
            throw std::runtime_error("[proc] 写入失败，子进程可能已退出");
        }
        p    += written;
        left -= written;
    }
}

std::size_t Subprocess::read(char* out, std::size_t cap) {
    DWORD got = 0;
    const BOOL ok = ReadFile(reinterpret_cast<HANDLE>(out_fd_), out,
                             static_cast<DWORD>(cap), &got, nullptr);
    if (!ok) {
        const DWORD e = GetLastError();
        if (e == ERROR_BROKEN_PIPE) return 0;  // 等价于 EOF
        throw std::runtime_error("[proc] 读取失败: " + os_error_text(static_cast<int>(e)));
    }
    return got;
}

bool Subprocess::wait_readable(int timeout_ms) {
    if (!out_fd_) return false;

    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
    for (;;) {
        DWORD avail = 0;
        if (PeekNamedPipe(reinterpret_cast<HANDLE>(out_fd_), nullptr, 0, nullptr, &avail, nullptr)) {
            if (avail > 0) return true;
        } else {
            return true;  // 管道已断，返回 true 让上层去 read() 拿 EOF
        }
        if (std::chrono::steady_clock::now() >= deadline) return false;
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
}

void Subprocess::close_stdin() {
    close_handle(in_fd_);
    stdin_open_ = false;
}

int Subprocess::wait(int timeout_ms) {
    std::lock_guard<std::mutex> lk(wait_mu_);
    if (reaped_) return cached_exit_;
    if (!process_handle_) return -1;

    const DWORD ms = timeout_ms < 0 ? INFINITE : static_cast<DWORD>(timeout_ms);
    const DWORD rc = WaitForSingleObject(reinterpret_cast<HANDLE>(process_handle_), ms);
    if (rc != WAIT_OBJECT_0) return -1;  // 超时

    DWORD code = 0;
    GetExitCodeProcess(reinterpret_cast<HANDLE>(process_handle_), &code);
    cached_exit_ = static_cast<int>(code);
    reaped_      = true;
    running_     = false;
    return cached_exit_;
}

void Subprocess::kill() {
    if (!process_handle_) return;
    TerminateProcess(reinterpret_cast<HANDLE>(process_handle_), 1);
    WaitForSingleObject(reinterpret_cast<HANDLE>(process_handle_), 2000);
    running_ = false;
}

void Subprocess::cleanup() noexcept {
    close_handle(in_fd_);
    close_handle(out_fd_);
    if (process_handle_) {
        if (running_) {
            TerminateProcess(reinterpret_cast<HANDLE>(process_handle_), 1);
            WaitForSingleObject(reinterpret_cast<HANDLE>(process_handle_), 2000);
        }
        CloseHandle(reinterpret_cast<HANDLE>(process_handle_));
        process_handle_ = 0;
    }
    running_ = false;
    pid_     = 0;
}

std::string Subprocess::describe() const {
    return "pid=" + std::to_string(pid_);
}

Subprocess::~Subprocess() { cleanup(); }

#endif  // _WIN32

}  // namespace fp
