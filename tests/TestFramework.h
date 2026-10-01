// 一个 80 行的单元测试框架。
//
// 为什么不用 GoogleTest / Catch2：
//   本项目的测试量在 60 个用例左右，用到的能力只有"断言 + 计数 + 报告"。
//   为这点需求引入一个需要联网 FetchContent 的依赖，会给"clone 下来就能构建"
//   这件事增加一个真实的失败点（本项目在 WSL 里就吃过 FetchContent 拉不动的亏）。
//   什么时候该换：需要参数化测试、mock 框架、或者测试数量上到几百个的时候。
#pragma once

#include <cmath>
#include <exception>
#include <functional>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

namespace fp::test {

struct Case {
    std::string           suite;
    std::string           name;
    std::function<void()> fn;
};

inline std::vector<Case>& registry() {
    static std::vector<Case> r;
    return r;
}

struct Registrar {
    Registrar(std::string suite, std::string name, std::function<void()> fn) {
        registry().push_back({std::move(suite), std::move(name), std::move(fn)});
    }
};

class Failure : public std::exception {
public:
    explicit Failure(std::string msg) : msg_(std::move(msg)) {}
    const char* what() const noexcept override { return msg_.c_str(); }

private:
    std::string msg_;
};

inline int run_all(const std::string& filter = std::string()) {
    int         passed = 0;
    int         failed = 0;
    std::string current_suite;

    for (const auto& c : registry()) {
        const std::string full = c.suite + "." + c.name;
        if (!filter.empty() && full.find(filter) == std::string::npos) continue;

        if (c.suite != current_suite) {
            current_suite = c.suite;
            std::cout << "\n[" << current_suite << "]\n";
        }

        try {
            c.fn();
            ++passed;
            std::cout << "  ok    " << c.name << "\n";
        } catch (const Failure& f) {
            ++failed;
            std::cout << "  FAIL  " << c.name << "\n        " << f.what() << "\n";
        } catch (const std::exception& e) {
            ++failed;
            std::cout << "  FAIL  " << c.name << "  (未捕获异常: " << e.what() << ")\n";
        } catch (...) {
            ++failed;
            std::cout << "  FAIL  " << c.name << "  (未捕获的非标准异常)\n";
        }
    }

    const int total = passed + failed;
    std::cout << "\n";
    if (total == 0) {
        std::cout << "没有匹配的用例（过滤词: " << filter << "）\n";
        return 1;
    }
    std::cout << (failed == 0 ? "全部通过" : "存在失败") << ": " << passed << " 通过 / "
              << failed << " 失败 / 共 " << total << " 个用例\n";
    return failed == 0 ? 0 : 1;
}

}  // namespace fp::test

// ── 用例定义 ──────────────────────────────────────────────
//
// 唯一标识符用 __LINE__ 拼出来，而不是用 name —— 因为 name 是给人看的中文，
// 中文和空格都不能出现在 C++ 标识符里。这样写还有个附带好处：
// 改测试名不会导致重复定义。
//
// 两层宏是必需的：__LINE__ 必须先展开成数字，才能和前后缀拼接。

#define FP_TEST_IMPL(suite, name, line)                                       \
    static void fp_test_##suite##_##line();                                   \
    static ::fp::test::Registrar fp_reg_##suite##_##line(                     \
        #suite, name, &fp_test_##suite##_##line);                             \
    static void fp_test_##suite##_##line()

#define FP_TEST_EXPAND(suite, name, line) FP_TEST_IMPL(suite, name, line)
#define FP_TEST(suite, name) FP_TEST_EXPAND(suite, name, __LINE__)

// ── 断言 ──────────────────────────────────────────────────

#define FP_CHECK(cond)                                                        \
    do {                                                                      \
        if (!(cond)) {                                                        \
            std::ostringstream _oss;                                          \
            _oss << __FILE__ << ":" << __LINE__ << "  断言不成立: " #cond;    \
            throw ::fp::test::Failure(_oss.str());                            \
        }                                                                     \
    } while (false)

#define FP_CHECK_MSG(cond, msg)                                               \
    do {                                                                      \
        if (!(cond)) {                                                        \
            std::ostringstream _oss;                                          \
            _oss << __FILE__ << ":" << __LINE__ << "  断言不成立: " #cond     \
                 << "\n        说明: " << (msg);                              \
            throw ::fp::test::Failure(_oss.str());                            \
        }                                                                     \
    } while (false)

#define FP_CHECK_EQ(a, b)                                                     \
    do {                                                                      \
        const auto _a = (a);                                                  \
        const auto _b = (b);                                                  \
        if (!(_a == _b)) {                                                    \
            std::ostringstream _oss;                                          \
            _oss << __FILE__ << ":" << __LINE__ << "  期望相等\n"             \
                 << "        实际: " << _a << "\n"                            \
                 << "        期望: " << _b;                                   \
            throw ::fp::test::Failure(_oss.str());                            \
        }                                                                     \
    } while (false)

#define FP_CHECK_NEAR(a, b, eps)                                              \
    do {                                                                      \
        const double _a = static_cast<double>(a);                             \
        const double _b = static_cast<double>(b);                             \
        if (std::fabs(_a - _b) > (eps)) {                                     \
            std::ostringstream _oss;                                          \
            _oss << __FILE__ << ":" << __LINE__ << "  数值偏差超容差\n"       \
                 << "        实际: " << _a << "\n"                            \
                 << "        期望: " << _b << "  容差 " << (eps);             \
            throw ::fp::test::Failure(_oss.str());                            \
        }                                                                     \
    } while (false)

#define FP_CHECK_THROWS(expr, ExcType)                                        \
    do {                                                                      \
        bool _thrown = false;                                                 \
        try {                                                                 \
            (void)(expr);                                                     \
        } catch (const ExcType&) {                                            \
            _thrown = true;                                                   \
        } catch (...) {                                                       \
            std::ostringstream _oss;                                          \
            _oss << __FILE__ << ":" << __LINE__                               \
                 << "  抛出了意料之外的异常类型: " #expr;                     \
            throw ::fp::test::Failure(_oss.str());                            \
        }                                                                     \
        if (!_thrown) {                                                       \
            std::ostringstream _oss;                                          \
            _oss << __FILE__ << ":" << __LINE__ << "  期望抛出 " #ExcType     \
                 << " 但没有: " #expr;                                        \
            throw ::fp::test::Failure(_oss.str());                            \
        }                                                                     \
    } while (false)
