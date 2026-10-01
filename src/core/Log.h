// FinPulse Terminal — 进程内分级日志
//
// 一条硬性设计约束：日志只能写 stderr。
// 桥接层把子进程的 stdout 整条流交给了帧协议，任何一个多余的 printf
// 都会让帧边界错位，进而触发难以定位的解码失败。与其靠"大家记得别写"，
// 不如在 API 层面堵死 —— 这个文件里根本没有往 stdout 写的路径。
#pragma once

#include <cstdio>
#include <ctime>
#include <mutex>
#include <sstream>
#include <string>

namespace fp {

enum class LogLevel { Trace = 0, Debug, Info, Warn, Error };

namespace detail {

inline const char* level_tag(LogLevel l) {
    switch (l) {
        case LogLevel::Trace: return "TRACE";
        case LogLevel::Debug: return "DEBUG";
        case LogLevel::Info:  return " INFO";
        case LogLevel::Warn:  return " WARN";
        case LogLevel::Error: return "ERROR";
    }
    return "?????";
}

struct LoggerState {
    std::mutex mu;
    LogLevel   level{LogLevel::Info};
};

inline LoggerState& logger_state() {
    static LoggerState s;  // 函数内静态，避免静态初始化顺序问题
    return s;
}

inline std::string wall_clock_stamp() {
    using namespace std::chrono;
    const auto now = system_clock::now();
    const auto ms  = duration_cast<milliseconds>(now.time_since_epoch()) % 1000;
    const std::time_t t = system_clock::to_time_t(now);
    std::tm tm{};
#if defined(_WIN32)
    localtime_s(&tm, &t);
#else
    localtime_r(&t, &tm);
#endif
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%02d:%02d:%02d.%03d",
                  tm.tm_hour, tm.tm_min, tm.tm_sec, static_cast<int>(ms.count()));
    return buf;
}

}  // namespace detail

inline void log_write(LogLevel lvl, const std::string& tag, const std::string& msg) {
    auto& st = detail::logger_state();
    std::lock_guard<std::mutex> lk(st.mu);
    if (static_cast<int>(lvl) < static_cast<int>(st.level)) return;

    std::string line;
    line.reserve(tag.size() + msg.size() + 32);
    line += detail::wall_clock_stamp();
    line += " [";
    line += detail::level_tag(lvl);
    line += "] [";
    line += tag;
    line += "] ";
    line += msg;

    std::fputs(line.c_str(), stderr);
    std::fputc('\n', stderr);
    std::fflush(stderr);  // 崩溃现场必须完整落盘，日志不做批量缓冲
}

inline void log_set_level(LogLevel lvl) {
    auto& st = detail::logger_state();
    std::lock_guard<std::mutex> lk(st.mu);
    st.level = lvl;
}

inline LogLevel log_level() {
    auto& st = detail::logger_state();
    std::lock_guard<std::mutex> lk(st.mu);
    return st.level;
}

}  // namespace fp

#define FP_LOG(lvl, tag, expr)                              \
    do {                                                    \
        std::ostringstream _fp_oss;                         \
        _fp_oss << expr;                                    \
        ::fp::log_write((lvl), (tag), _fp_oss.str());       \
    } while (false)

#define FP_TRACE(tag, expr) FP_LOG(::fp::LogLevel::Trace, tag, expr)
#define FP_DEBUG(tag, expr) FP_LOG(::fp::LogLevel::Debug, tag, expr)
#define FP_INFO(tag, expr)  FP_LOG(::fp::LogLevel::Info,  tag, expr)
#define FP_WARN(tag, expr)  FP_LOG(::fp::LogLevel::Warn,  tag, expr)
#define FP_ERROR(tag, expr) FP_LOG(::fp::LogLevel::Error, tag, expr)
