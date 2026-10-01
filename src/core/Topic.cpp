#include "core/Topic.h"

#include <stdexcept>

namespace fp {

namespace {

std::vector<std::string> split_dots(const std::string& s) {
    std::vector<std::string> out;
    std::size_t start = 0;
    for (;;) {
        const std::size_t dot = s.find('.', start);
        if (dot == std::string::npos) {
            out.push_back(s.substr(start));
            break;
        }
        out.push_back(s.substr(start, dot - start));
        start = dot + 1;
    }
    return out;
}

}  // namespace

bool is_valid_topic_name(std::string_view s) {
    if (s.empty() || s.size() > 200) return false;
    if (s.front() == '.' || s.back() == '.') return false;

    bool prev_dot = false;
    for (const char c : s) {
        const unsigned char u = static_cast<unsigned char>(c);
        if ((u >= 'a' && u <= 'z') || (u >= 'A' && u <= 'Z') || (u >= '0' && u <= '9')) {
            prev_dot = false;
            continue;
        }
        switch (c) {
            case '_': case ':': case '-': case '@': case '$': case '*':
                prev_dot = false;
                continue;
            case '.':
                if (prev_dot) return false;  // 连续两个点会产生空段
                prev_dot = true;
                continue;
            default:
                return false;
        }
    }
    return true;
}

void Topic::split() {
    segs_ = split_dots(name_);
}

Topic::Topic(std::string name) {
    if (!is_valid_topic_name(name)) {
        throw std::invalid_argument("[topic] 非法主题名: '" + name + "'");
    }
    if (name.find('*') != std::string::npos) {
        throw std::invalid_argument("[topic] 主题名不能包含通配符: '" + name + "'");
    }
    name_ = std::move(name);
    split();
}

TopicPattern::TopicPattern(std::string pattern) : pat_(std::move(pattern)) {
    if (pat_.empty()) throw std::invalid_argument("[topic] 订阅模式不能为空");
    if (!is_valid_topic_name(pat_)) {
        throw std::invalid_argument("[topic] 非法订阅模式: '" + pat_ + "'");
    }

    segs_ = split_dots(pat_);
    for (const auto& seg : segs_) {
        if (seg.empty()) throw std::invalid_argument("[topic] 订阅模式含空段: '" + pat_ + "'");
    }

    for (std::size_t i = 0; i < segs_.size(); ++i) {
        if (segs_[i] == "**") {
            if (i + 1 != segs_.size()) {
                throw std::invalid_argument("[topic] '**' 只能出现在模式末尾: '" + pat_ + "'");
            }
            tail_glob_ = true;
        } else if (segs_[i].find('*') != std::string::npos && segs_[i] != "*") {
            // 不支持 a*b 这种段内部分通配 —— 那等价于引正则，收益为零
            throw std::invalid_argument("[topic] 不支持段内部分通配: '" + pat_ + "'");
        }
    }
}

bool TopicPattern::matches(const Topic& t) const {
    const auto& ts = t.segments();
    const std::size_t n = segs_.size();
    const std::size_t m = ts.size();
    const std::size_t fixed = tail_glob_ ? n - 1 : n;

    if (tail_glob_) {
        if (m < fixed) return false;   // '**' 之前的部分必须逐段对上
    } else if (m != n) {
        return false;
    }

    for (std::size_t i = 0; i < fixed; ++i) {
        if (segs_[i] == "*") continue;
        if (segs_[i] != ts[i]) return false;
    }
    return true;
}

std::vector<std::string> TopicPattern::capture(const Topic& t) const {
    std::vector<std::string> out;
    if (!matches(t)) return out;

    const auto& ts = t.segments();
    const std::size_t fixed = tail_glob_ ? segs_.size() - 1 : segs_.size();

    for (std::size_t i = 0; i < fixed; ++i) {
        if (segs_[i] == "*") out.push_back(ts[i]);
    }
    if (tail_glob_) {
        std::string joined;
        for (std::size_t i = fixed; i < ts.size(); ++i) {
            if (!joined.empty()) joined.push_back('.');
            joined += ts[i];
        }
        out.push_back(joined);  // '**' 的捕获结果压成一个点分字符串
    }
    return out;
}

}  // namespace fp
