#include "core/Json.h"

#include <cctype>
#include <cerrno>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace fp {

namespace {

const Json kNull{};

[[noreturn]] void fail(const char* msg, std::size_t off) {
    throw JsonError(std::string("[json] ") + msg, off);
}

// UTF-8 编码一个码点。\uXXXX 解析出来后走这里落成字节。
void append_utf8(std::string& out, unsigned cp) {
    if (cp <= 0x7F) {
        out.push_back(static_cast<char>(cp));
    } else if (cp <= 0x7FF) {
        out.push_back(static_cast<char>(0xC0 | (cp >> 6)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else if (cp <= 0xFFFF) {
        out.push_back(static_cast<char>(0xE0 | (cp >> 12)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else {
        out.push_back(static_cast<char>(0xF0 | (cp >> 18)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 12) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    }
}

// 浮点最短往返表示。
// 直接上 %.17g 会让 0.1 输出成 0.10000000000000001，日志和协议都很难看；
// 先试 %.15g，回读不相等再升精度。这是大多数 JSON 库的做法。
std::string format_double(double v) {
    if (std::isnan(v) || std::isinf(v)) return "null";  // JSON 规范没有 NaN/Inf
    char buf[40];
    std::snprintf(buf, sizeof(buf), "%.15g", v);
    if (std::strtod(buf, nullptr) != v) {
        std::snprintf(buf, sizeof(buf), "%.17g", v);
    }
    return buf;
}

void dump_string(std::string& out, const std::string& s) {
    out.push_back('"');
    for (unsigned char c : s) {
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n";  break;
            case '\r': out += "\\r";  break;
            case '\t': out += "\\t";  break;
            case '\b': out += "\\b";  break;
            case '\f': out += "\\f";  break;
            default:
                if (c < 0x20) {
                    char esc[8];
                    std::snprintf(esc, sizeof(esc), "\\u%04x", c);
                    out += esc;
                } else {
                    // 非 ASCII 直接透传 UTF-8，不做无谓转义
                    out.push_back(static_cast<char>(c));
                }
        }
    }
    out.push_back('"');
}

// ── 递归下降解析器 ────────────────────────────────────────
class Parser {
public:
    explicit Parser(std::string_view src) : src_(src) {}

    Json parse_document() {
        skip_ws();
        Json v = parse_value(0);
        skip_ws();
        if (pos_ != src_.size()) fail("文档结尾存在多余字符", pos_);
        return v;
    }

private:
    static constexpr int kMaxDepth = 64;  // 病态深嵌套会把调用栈打爆，这里设个上限

    void skip_ws() {
        while (pos_ < src_.size()) {
            const char c = src_[pos_];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') ++pos_;
            else break;
        }
    }

    char peek() const { return pos_ < src_.size() ? src_[pos_] : '\0'; }

    void expect(char c, const char* what) {
        if (peek() != c) fail(what, pos_);
        ++pos_;
    }

    Json parse_value(int depth) {
        if (depth > kMaxDepth) fail("嵌套层数超过上限", pos_);
        switch (peek()) {
            case '{': return parse_object(depth);
            case '[': return parse_array(depth);
            case '"': return Json(parse_string());
            case 't': return parse_literal("true", Json(true));
            case 'f': return parse_literal("false", Json(false));
            case 'n': return parse_literal("null", Json(nullptr));
            default:  return parse_number();
        }
    }

    Json parse_literal(const char* lit, Json v) {
        const std::size_t n = std::strlen(lit);
        if (src_.compare(pos_, n, lit) != 0) fail("非法字面量", pos_);
        pos_ += n;
        return v;
    }

    Json parse_object(int depth) {
        expect('{', "对象应以 { 开始");
        Json obj = Json::object();
        skip_ws();
        if (peek() == '}') { ++pos_; return obj; }
        for (;;) {
            skip_ws();
            if (peek() != '"') fail("对象键必须是字符串", pos_);
            std::string key = parse_string();
            skip_ws();
            expect(':', "对象键后应跟 :");
            skip_ws();
            obj.set(std::move(key), parse_value(depth + 1));
            skip_ws();
            if (peek() == ',') { ++pos_; continue; }
            expect('}', "对象应以 } 结束");
            return obj;
        }
    }

    Json parse_array(int depth) {
        expect('[', "数组应以 [ 开始");
        Json arr = Json::array();
        skip_ws();
        if (peek() == ']') { ++pos_; return arr; }
        for (;;) {
            skip_ws();
            arr.push(parse_value(depth + 1));
            skip_ws();
            if (peek() == ',') { ++pos_; continue; }
            expect(']', "数组应以 ] 结束");
            return arr;
        }
    }

    unsigned parse_hex4() {
        unsigned cp = 0;
        for (int k = 0; k < 4; ++k) {
            if (pos_ >= src_.size()) fail("\\u 转义不完整", pos_);
            const char c = src_[pos_++];
            cp <<= 4;
            if (c >= '0' && c <= '9')      cp |= static_cast<unsigned>(c - '0');
            else if (c >= 'a' && c <= 'f') cp |= static_cast<unsigned>(c - 'a' + 10);
            else if (c >= 'A' && c <= 'F') cp |= static_cast<unsigned>(c - 'A' + 10);
            else fail("\\u 转义含非法十六进制字符", pos_ - 1);
        }
        return cp;
    }

    std::string parse_string() {
        expect('"', "字符串应以 \" 开始");
        std::string out;
        for (;;) {
            if (pos_ >= src_.size()) fail("字符串未闭合", pos_);
            const unsigned char c = static_cast<unsigned char>(src_[pos_++]);
            if (c == '"') return out;
            if (c != '\\') {
                if (c < 0x20) fail("字符串内出现未转义的控制字符", pos_ - 1);
                out.push_back(static_cast<char>(c));
                continue;
            }
            if (pos_ >= src_.size()) fail("转义符后缺少字符", pos_);
            const char e = src_[pos_++];
            switch (e) {
                case '"':  out.push_back('"');  break;
                case '\\': out.push_back('\\'); break;
                case '/':  out.push_back('/');  break;
                case 'b':  out.push_back('\b'); break;
                case 'f':  out.push_back('\f'); break;
                case 'n':  out.push_back('\n'); break;
                case 'r':  out.push_back('\r'); break;
                case 't':  out.push_back('\t'); break;
                case 'u': {
                    unsigned cp = parse_hex4();
                    // UTF-16 代理对：高位之后必须是 \uDC00-\uDFFF，合并成 4 字节 UTF-8
                    if (cp >= 0xD800 && cp <= 0xDBFF) {
                        if (pos_ + 1 < src_.size() && src_[pos_] == '\\' && src_[pos_ + 1] == 'u') {
                            pos_ += 2;
                            const unsigned lo = parse_hex4();
                            if (lo >= 0xDC00 && lo <= 0xDFFF) {
                                cp = 0x10000u + ((cp - 0xD800u) << 10) + (lo - 0xDC00u);
                            } else {
                                fail("代理对低位不合法", pos_ - 4);
                            }
                        } else {
                            fail("代理对缺少低位", pos_);
                        }
                    }
                    append_utf8(out, cp);
                    break;
                }
                default: fail("未知转义序列", pos_ - 1);
            }
        }
    }

    Json parse_number() {
        const std::size_t start = pos_;
        bool is_int = true;

        if (peek() == '-') ++pos_;
        if (pos_ >= src_.size() || !std::isdigit(static_cast<unsigned char>(src_[pos_])))
            fail("非法数值", start);
        while (pos_ < src_.size() && std::isdigit(static_cast<unsigned char>(src_[pos_]))) ++pos_;
        if (peek() == '.') {
            is_int = false;
            ++pos_;
            if (pos_ >= src_.size() || !std::isdigit(static_cast<unsigned char>(src_[pos_])))
                fail("小数点后缺少数字", pos_);
            while (pos_ < src_.size() && std::isdigit(static_cast<unsigned char>(src_[pos_]))) ++pos_;
        }
        if (peek() == 'e' || peek() == 'E') {
            is_int = false;
            ++pos_;
            if (peek() == '+' || peek() == '-') ++pos_;
            if (pos_ >= src_.size() || !std::isdigit(static_cast<unsigned char>(src_[pos_])))
                fail("指数部分缺少数字", pos_);
            while (pos_ < src_.size() && std::isdigit(static_cast<unsigned char>(src_[pos_]))) ++pos_;
        }

        const std::string tok(src_.substr(start, pos_ - start));
        if (is_int) {
            errno = 0;
            char* end = nullptr;
            const long long v = std::strtoll(tok.c_str(), &end, 10);
            // 溢出 int64（比如某些交易所返回的超大编号）时降级为 double，比直接抛错对调用方友好
            if (errno == 0 && end && *end == '\0') return Json(v);
        }
        return Json(std::strtod(tok.c_str(), nullptr));
    }

    std::string_view src_;
    std::size_t      pos_{0};
};

}  // namespace

const Json& json_null() { return kNull; }

const char* Json::type_name() const noexcept {
    switch (type_) {
        case Type::Null:   return "null";
        case Type::Bool:   return "bool";
        case Type::Number: return "number";
        case Type::String: return "string";
        case Type::Array:  return "array";
        case Type::Object: return "object";
    }
    return "?";
}

bool Json::as_bool() const {
    if (type_ != Type::Bool) throw JsonTypeError(std::string("期望 bool，实际 ") + type_name());
    return b_;
}

long long Json::as_int() const {
    if (type_ != Type::Number) throw JsonTypeError(std::string("期望 number，实际 ") + type_name());
    return is_int_ ? i_ : static_cast<long long>(d_);
}

double Json::as_double() const {
    if (type_ != Type::Number) throw JsonTypeError(std::string("期望 number，实际 ") + type_name());
    return d_;
}

const std::string& Json::as_string() const {
    if (type_ != Type::String) throw JsonTypeError(std::string("期望 string，实际 ") + type_name());
    return s_;
}

std::size_t Json::size() const noexcept {
    if (type_ == Type::Array)  return arr_.size();
    if (type_ == Type::Object) return obj_.size();
    return 0;
}

const Json& Json::at(std::size_t i) const {
    if (type_ != Type::Array) throw JsonTypeError(std::string("期望 array，实际 ") + type_name());
    if (i >= arr_.size()) throw std::out_of_range("[json] 数组下标越界");
    return arr_[i];
}

Json& Json::at(std::size_t i) {
    if (type_ != Type::Array) throw JsonTypeError(std::string("期望 array，实际 ") + type_name());
    if (i >= arr_.size()) throw std::out_of_range("[json] 数组下标越界");
    return arr_[i];
}

// 字段不存在返回 Null 单例，这样 `msg["params"]["symbol"]` 这种链式读法不会中断；
// 真正的"字段必须有"由调用方的 as_xxx() 严格取值来兜底。
const Json& Json::at(std::string_view key) const {
    if (type_ != Type::Object) return kNull;
    for (const auto& kv : obj_) {
        if (kv.first == key) return kv.second;
    }
    return kNull;
}

Json& Json::operator[](std::string_view key) {
    ensure_object();
    for (auto& kv : obj_) {
        if (kv.first == key) return kv.second;
    }
    obj_.emplace_back(std::string(key), Json{});
    return obj_.back().second;
}

bool Json::has(std::string_view key) const {
    if (type_ != Type::Object) return false;
    for (const auto& kv : obj_) {
        if (kv.first == key) return true;
    }
    return false;
}

void Json::ensure_object() {
    if (type_ != Type::Object) { type_ = Type::Object; obj_.clear(); }
}

void Json::ensure_array() {
    if (type_ != Type::Array) { type_ = Type::Array; arr_.clear(); }
}

void Json::push(Json v) {
    ensure_array();
    arr_.push_back(std::move(v));
}

void Json::set(std::string key, Json v) {
    ensure_object();
    for (auto& kv : obj_) {
        if (kv.first == key) { kv.second = std::move(v); return; }
    }
    obj_.emplace_back(std::move(key), std::move(v));
}

void Json::dump_to(std::string& out, int indent, int depth) const {
    const bool pretty = indent > 0;

    switch (type_) {
        case Type::Null:   out += "null"; break;
        case Type::Bool:   out += b_ ? "true" : "false"; break;
        case Type::Number:
            if (is_int_) out += std::to_string(i_);
            else         out += format_double(d_);
            break;
        case Type::String:
            dump_string(out, s_);
            break;
        case Type::Array: {
            if (arr_.empty()) { out += "[]"; break; }
            out.push_back('[');
            for (std::size_t n = 0; n < arr_.size(); ++n) {
                if (n) out.push_back(',');
                if (pretty) { out.push_back('\n'); out.append(static_cast<std::size_t>(indent) * (depth + 1), ' '); }
                arr_[n].dump_to(out, indent, depth + 1);
            }
            if (pretty) { out.push_back('\n'); out.append(static_cast<std::size_t>(indent) * depth, ' '); }
            out.push_back(']');
            break;
        }
        case Type::Object: {
            if (obj_.empty()) { out += "{}"; break; }
            out.push_back('{');
            for (std::size_t n = 0; n < obj_.size(); ++n) {
                if (n) out.push_back(',');
                if (pretty) { out.push_back('\n'); out.append(static_cast<std::size_t>(indent) * (depth + 1), ' '); }
                dump_string(out, obj_[n].first);
                out += pretty ? ": " : ":";
                obj_[n].second.dump_to(out, indent, depth + 1);
            }
            if (pretty) { out.push_back('\n'); out.append(static_cast<std::size_t>(indent) * depth, ' '); }
            out.push_back('}');
            break;
        }
    }
}

std::string Json::dump(int indent) const {
    std::string out;
    out.reserve(256);
    dump_to(out, indent, 0);
    return out;
}

Json Json::parse(std::string_view text) {
    Parser p(text);
    return p.parse_document();
}

}  // namespace fp
