// FinPulse Terminal — 受限 JSON 实现
//
// 为什么不用 nlohmann/json：
//   1. 它是个 25k 行的纯头文件库，编译进本项目会让单 TU 编译时间从 0.4s 涨到 3s+；
//   2. 发布包要把 Python 引擎一起打进去，希望二进制尽量小；
//   3. 桥接协议只用到 标量/数组/对象 三类结构，不需要它 90% 的能力。
// 代价：不支持注释、NaN/Inf、自定义分配器；对象字段查找是 O(n)（字段数都是个位数，实测无感）。
//
// 不支持 \uXXXX 之外的转义扩展；数值内部按 double 存储，整数另存一份 ll 以保精度。
#pragma once

#include <cstddef>
#include <memory>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace fp {

/// 解析失败时抛出，携带出错字节偏移，便于定位协议里的坏帧。
class JsonError : public std::runtime_error {
public:
    JsonError(std::string msg, std::size_t offset)
        : std::runtime_error(std::move(msg)), offset_(offset) {}

    std::size_t offset() const noexcept { return offset_; }

private:
    std::size_t offset_;
};

/// 类型不符时抛出，避免 "读错了字段静默拿到 0" 这类难查的 bug。
class JsonTypeError : public std::runtime_error {
public:
    explicit JsonTypeError(std::string msg) : std::runtime_error(std::move(msg)) {}
};

class Json {
public:
    enum class Type { Null, Bool, Number, String, Array, Object };

    // ── 构造 ──────────────────────────────────────────────
    Json() noexcept = default;
    Json(std::nullptr_t) noexcept {}
    Json(bool v) noexcept : type_(Type::Bool), b_(v) {}
    // 整数重载要覆盖 int / long / long long / 无符号各档宽度。
    // 少了 long 那一档，传 int64_t（在 Linux 上就是 long）会因为
    // 能隐式转成 int、long long、unsigned long long、double 而变成歧义调用。
    Json(int v) noexcept : type_(Type::Number), i_(v), d_(static_cast<double>(v)), is_int_(true) {}
    Json(unsigned v) noexcept : type_(Type::Number), i_(v), d_(static_cast<double>(v)), is_int_(true) {}
    Json(long v) noexcept : type_(Type::Number), i_(v), d_(static_cast<double>(v)), is_int_(true) {}
    Json(unsigned long v) noexcept
        : type_(Type::Number), i_(static_cast<long long>(v)), d_(static_cast<double>(v)), is_int_(true) {}
    Json(long long v) noexcept : type_(Type::Number), i_(v), d_(static_cast<double>(v)), is_int_(true) {}
    Json(unsigned long long v) noexcept
        : type_(Type::Number), i_(static_cast<long long>(v)), d_(static_cast<double>(v)), is_int_(true) {}
    Json(double v) noexcept : type_(Type::Number), d_(v) {}
    Json(const char* v) : type_(Type::String), s_(v ? v : "") {}
    Json(std::string v) : type_(Type::String), s_(std::move(v)) {}
    Json(std::string_view v) : type_(Type::String), s_(v) {}

    static Json array() { Json j; j.type_ = Type::Array; return j; }
    static Json object() { Json j; j.type_ = Type::Object; return j; }

    // ── 类型查询 ──────────────────────────────────────────
    Type type() const noexcept { return type_; }
    const char* type_name() const noexcept;

    bool is_null()   const noexcept { return type_ == Type::Null; }
    bool is_bool()   const noexcept { return type_ == Type::Bool; }
    bool is_number() const noexcept { return type_ == Type::Number; }
    bool is_string() const noexcept { return type_ == Type::String; }
    bool is_array()  const noexcept { return type_ == Type::Array; }
    bool is_object() const noexcept { return type_ == Type::Object; }
    /// 数值是否是整型（或整值的浮点）。成交量、笔数这类字段用它判断。
    bool is_integral() const noexcept { return type_ == Type::Number && is_int_; }

    // ── 严格取值（类型不符抛异常）─────────────────────────
    bool               as_bool()   const;
    long long          as_int()    const;
    double             as_double() const;
    const std::string& as_string() const;

    // ── 宽松取值（用于可选字段，缺省即回落）────────────────
    bool as_bool_or(bool d) const noexcept { return type_ == Type::Bool ? b_ : d; }
    long long as_int_or(long long d) const noexcept {
        if (type_ != Type::Number) return d;
        return is_int_ ? i_ : static_cast<long long>(d_);
    }
    double as_double_or(double d) const noexcept { return type_ == Type::Number ? d_ : d; }
    std::string as_string_or(std::string d) const { return type_ == Type::String ? s_ : std::move(d); }

    // ── 容器访问 ──────────────────────────────────────────
    std::size_t size() const noexcept;

    /// 数组下标。越界抛 std::out_of_range。
    const Json& at(std::size_t i) const;
    Json&       at(std::size_t i);

    /// 对象字段。字段不存在返回 Null 单例（便于链式可选字段读取 `j["a"]["b"]`）。
    const Json& at(std::string_view key) const;
    const Json& operator[](std::string_view key) const { return at(key); }
    /// 用于写入；当前不是对象时会先自转为对象。
    Json& operator[](std::string_view key);

    bool has(std::string_view key) const;

    // ── 修改 ──────────────────────────────────────────────
    /// 追加到数组；当前不是数组则先自转为数组。
    void push(Json v);
    /// 设置对象字段（已存在则覆盖）。
    void set(std::string key, Json v);

    /// 遍历对象字段（保持插入顺序）。
    const std::vector<std::pair<std::string, Json>>& members() const { return obj_; }
    /// 遍历数组元素。
    const std::vector<Json>& items() const { return arr_; }

    // ── 序列化 / 解析 ─────────────────────────────────────
    /// indent < 0 输出紧凑格式；否则每层缩进 indent 个空格。
    std::string dump(int indent = -1) const;
    static Json parse(std::string_view text);

private:
    void ensure_object();
    void ensure_array();
    void dump_to(std::string& out, int indent, int depth) const;

    Type type_{Type::Null};

    bool      b_{false};
    long long i_{0};
    double    d_{0.0};
    bool      is_int_{false};

    std::string                                s_;
    std::vector<Json>                          arr_;
    std::vector<std::pair<std::string, Json>>  obj_;
};

/// 全局 Null 单例，供 at() 在字段缺失时返回。
const Json& json_null();

}  // namespace fp
