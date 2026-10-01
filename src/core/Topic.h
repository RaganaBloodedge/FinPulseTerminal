// FinPulse Terminal — 主题（Topic）与订阅模式
//
// 主题是一串以 '.' 分隔的层级名，例如：
//     market.quote.AAPL
//     market.kline.AAPL.1m
//     engine.status
//
// 订阅模式支持两种通配：
//     *   匹配一层          market.quote.*        → market.quote.AAPL
//     **  匹配尾部剩余所有层  market.**             → market.quote.AAPL.深度随便
// 其中 ** 只允许出现在模式末尾。这个限制不是偷懒：允许 ** 出现在中间会让
// 匹配退化成正则表达式级别的复杂度，而实际用法里从来没有这个需求。
#pragma once

#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

namespace fp {

/// 主题名。构造时做字符集校验，脏主题名（空格、控制字符、超长）在入口就被挡掉，
/// 避免它们一路流到日志和协议里再排查。
class Topic {
public:
    Topic() = default;
    /// 非法主题名（空、超长、含非法字符、含通配符）抛 std::invalid_argument。
    explicit Topic(std::string name);

    /// 内部高频路径用：跳过校验，调用方保证名字合法。
    static Topic unchecked(std::string name) {
        Topic t;
        t.name_ = std::move(name);
        t.split();
        return t;
    }

    const std::string&              name() const noexcept { return name_; }
    const std::vector<std::string>& segments() const noexcept { return segs_; }
    bool                            empty() const noexcept { return name_.empty(); }
    std::size_t                     depth() const noexcept { return segs_.size(); }

    bool operator==(const Topic& o) const noexcept { return name_ == o.name_; }
    bool operator!=(const Topic& o) const noexcept { return !(*this == o); }
    bool operator<(const Topic& o) const noexcept { return name_ < o.name_; }

private:
    void split();

    std::string              name_;
    std::vector<std::string> segs_;
};

/// 订阅模式。
class TopicPattern {
public:
    TopicPattern() = default;
    /// 非法模式（空、** 不在末尾、段为空）抛 std::invalid_argument。
    explicit TopicPattern(std::string pattern);

    const std::string& pattern() const noexcept { return pat_; }
    bool               tail_glob() const noexcept { return tail_glob_; }

    bool matches(const Topic& t) const;
    /// 按顺序返回各通配符捕获到的段；* 捕获 1 段，** 捕获剩余全部。
    std::vector<std::string> capture(const Topic& t) const;

private:
    std::string              pat_;
    std::vector<std::string> segs_;
    bool                     tail_glob_{false};
};

/// 主题名字符集：字母、数字、下划线、点、冒号、连字符、@、$。
/// 交易所符号里偶尔出现 '.'（如 BRK.B），此时按设计会被当成两层，
/// 所以约定：含点的符号在入总线前替换为 '-'。
bool is_valid_topic_name(std::string_view s);

}  // namespace fp
