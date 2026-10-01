// FinPulse Terminal — 对话记录渲染测试
//
// 这一层全是纯函数，但它产生的字符串会被 Qt 的**富文本解析器**读一遍。
// 那意味着两类缺陷在源码里看不出来、在界面上却瞒不住：
//
//   1. **转义漏了**：报告里出现一个 `<`（"样本 < 60 根"、"RSI > 70 < 80"
//      这类句子在金融文本里到处都是），Qt 会把它当标签的开始，于是后面
//      整段文字被吞掉 —— 屏幕上看就是"回答少了一半"，而且不报任何错。
//   2. **气泡结构拼错**：少一个 </td> 会让后面所有消息挤进同一格，
//      看起来像"消息丢了"。
//
// 所以这里断言的重点不是"好不好看"，而是：
//   * 任何进入 HTML 的文本都被转义；
//   * 我们自己插的标签**数量配对**；
//   * 转义之后再做 Markdown 替换，顺序不会把用户内容也吃进去。

#include "TestFramework.h"

#include "gui/ChatRender.h"

#include <string>

using namespace fp::chat;

namespace {

/// 数一个子串出现几次。用来断言标签配对。
std::size_t count(const std::string& hay, const std::string& needle) {
    std::size_t n = 0, pos = 0;
    while ((pos = hay.find(needle, pos)) != std::string::npos) {
        ++n;
        pos += needle.size();
    }
    return n;
}

}  // namespace

// ── escape ────────────────────────────────────────────────

FP_TEST(chatrender, "转义覆盖全部五类危险字符") {
    FP_CHECK_EQ(escape("a&b"), std::string("a&amp;b"));
    FP_CHECK_EQ(escape("a<b"), std::string("a&lt;b"));
    FP_CHECK_EQ(escape("a>b"), std::string("a&gt;b"));
    FP_CHECK_EQ(escape("a\"b"), std::string("a&quot;b"));
    FP_CHECK_EQ(escape("a'b"), std::string("a&#39;b"));
}

FP_TEST(chatrender, "转义不动普通文本") {
    // 中文、数字、常见标点都该原样过去。多转一点点就会在界面上看到
    // 一堆 &amp; 之类的实体名，比不转义还糟。
    const std::string s = "贵州茅台 600519.SH 收盘 1680.50 元，涨幅 +1.23%（日内）";
    FP_CHECK_EQ(escape(s), s);
}

FP_TEST(chatrender, "转义是幂等的入口而不是幂等函数") {
    // 说明一件容易被误解的事：escape 不是幂等的（二次转义会把 & 再写成
    // &amp;）。所以它必须**无条件且只做一次**地当入口用，而不是"保险起见
    // 多调用一遍"。这条断言把这个事实固定下来，免得有人看到重复调用
    // 觉得无害。
    FP_CHECK_EQ(escape(escape("a<b")), std::string("a&amp;lt;b"));
}

// ── with_breaks ───────────────────────────────────────────

FP_TEST(chatrender, "换行变成 br 而不是被吃掉") {
    FP_CHECK_EQ(with_breaks("a\nb"), std::string("a<br/>b"));
}

// ── render_text ───────────────────────────────────────────

FP_TEST(chatrender, "报告里的尖括号不会吞掉后面整段") {
    // 这是这层最要紧的一条。真实报告里出现过 "训练样本 < 60 根" 与
    // "RSI > 70 < 80" 这种句子。
    const std::string html = render_text("样本 < 60 根，RSI > 70 < 80");
    FP_CHECK(html.find("&lt; 60") != std::string::npos);
    FP_CHECK(html.find("&gt; 70") != std::string::npos);
    // 反过来也要断言：**没有**裸的 < 或 > 漏出去。
    FP_CHECK(html.find("< 60") == std::string::npos);
    FP_CHECK(html.find("> 70") == std::string::npos);
}

FP_TEST(chatrender, "粗体标记转成 b 标签") {
    const std::string html = render_text("结论：**偏多**，注意回撤");
    FP_CHECK(html.find("<b>偏多</b>") != std::string::npos);
    // 两个星号必须都没了，否则界面上会看到残留的 **
    FP_CHECK(html.find("**") == std::string::npos);
}

FP_TEST(chatrender, "落单的星号原样保留") {
    // 半吊子解析器在这里会把 "2*3*4" 吃掉两段。宁可什么都不做。
    const std::string html = render_text("算式 2*3*4 和 5*6 都不该被当成强调");
    FP_CHECK(html.find("2*3*4") != std::string::npos);
    FP_CHECK(html.find("5*6") != std::string::npos);
    FP_CHECK(html.find("<b>") == std::string::npos);
}

FP_TEST(chatrender, "成对的星号全都能加粗") {
    const std::string html = render_text("**偏多** 与 **高波动** 并存");
    FP_CHECK_EQ(count(html, "<b>"), std::size_t(2));
    FP_CHECK(html.find("<b>偏多</b>") != std::string::npos);
    FP_CHECK(html.find("<b>高波动</b>") != std::string::npos);
}

FP_TEST(chatrender, "多余的星号不被吞掉") {
    // 三个标记 = 一对 + 落单一个。落单那个必须原样留在输出里 ——
    // "配不上对就一起删掉"会让模型写的内容凭空少一截，而这正是
    // 半吊子 Markdown 解析器最典型的破坏方式。
    const std::string html = render_text("**甲**乙**丙");
    FP_CHECK_EQ(count(html, "<b>"), std::size_t(1));
    FP_CHECK(html.find("<b>甲</b>") != std::string::npos);
    FP_CHECK(html.find("**丙") != std::string::npos);
}

FP_TEST(chatrender, "加粗里的尖括号仍然被转义") {
    // 顺序问题：先转义再找 **。反过来的话，`**` 匹配会看到已经被替换过的
    // 实体串，位置全乱，而且转义会把我们自己插的 <b> 也一起转掉。
    const std::string html = render_text("**<a>&b**");
    FP_CHECK(html.find("<b>&lt;a&gt;&amp;b</b>") != std::string::npos);
}

FP_TEST(chatrender, "行首井号当标题") {
    const std::string html = render_text("# 风险概览");
    FP_CHECK(html.find("<b>风险概览</b>") != std::string::npos);
    FP_CHECK(html.find("#") == std::string::npos);
}

FP_TEST(chatrender, "四个井号以上不算标题") {
    // 只支持 # ~ ####。再多就不是"标题层级"而是正文里的装饰井号了，
    // 认了反而会把用户内容改样。
    const std::string html = render_text("##### 五级");
    FP_CHECK(html.find("<b>") == std::string::npos);
    FP_CHECK(html.find("#####") != std::string::npos);
}

FP_TEST(chatrender, "行首短横线当列表项") {
    const std::string html = render_text("- 年化波动率 28.4%\n* 最大回撤 12.1%");
    FP_CHECK_EQ(count(html, "&nbsp;&nbsp;• "), std::size_t(2));
    FP_CHECK(html.find("年化波动率 28.4%") != std::string::npos);
    FP_CHECK(html.find("最大回撤 12.1%") != std::string::npos);
}

FP_TEST(chatrender, "每一行都包在 div 里且配对") {
    const std::string html = render_text("第一行\n\n第三行");
    FP_CHECK_EQ(count(html, "<div>"), count(html, "</div>"));
    FP_CHECK_EQ(count(html, "<div>"), std::size_t(3));   // 空行也占一行
}

FP_TEST(chatrender, "空行渲染成占位而不是塌掉") {
    // 塌掉的话段落之间会挤在一起，看起来像"模型没分段"。
    const std::string html = render_text("a\n\nb");
    FP_CHECK(html.find("&nbsp;") != std::string::npos);
}

FP_TEST(chatrender, "空文本不产生半截标签") {
    const std::string html = render_text("");
    FP_CHECK_EQ(count(html, "<div>"), count(html, "</div>"));
    FP_CHECK(html.find("<b>") == std::string::npos);
}

// ── bubble / pre_bubble ───────────────────────────────────

FP_TEST(chatrender, "气泡的表格结构配对完整") {
    const std::string html = bubble("你", "<div>hi</div>", "user");
    FP_CHECK_EQ(count(html, "<table"), count(html, "</table>"));
    FP_CHECK_EQ(count(html, "<tr>"), count(html, "</tr>"));
    FP_CHECK_EQ(count(html, "<td"), count(html, "</td>"));
    FP_CHECK_EQ(count(html, "<table"), std::size_t(1));
}

FP_TEST(chatrender, "气泡的语气决定底色") {
    FP_CHECK(bubble("a", "", "user").find("#eef4ff") != std::string::npos);
    FP_CHECK(bubble("a", "", "assistant").find("#f7fbf7") != std::string::npos);
    FP_CHECK(bubble("a", "", "error").find("#fff4f4") != std::string::npos);
    // 认不出的语气要退到一个确定的默认值，而不是留下空串让 Qt 去猜。
    FP_CHECK(bubble("a", "", "什么鬼").find("bgcolor=\"#f3f4f6\"") != std::string::npos);
}

FP_TEST(chatrender, "气泡的发言人也转义") {
    // 角色名来自配置文件，里面完全可能出现 & 或 <。
    const std::string html = bubble("<script>", "x", "system");
    FP_CHECK(html.find("&lt;script&gt;") != std::string::npos);
    FP_CHECK(html.find("<script>") == std::string::npos);
}

FP_TEST(chatrender, "等宽气泡保留换行且不改写") {
    // 投票表靠空格对齐，换行必须原样留给 <pre> —— 转成 <br/> 就还要
    // 靠浏览器换行，对齐会散。
    const std::string raw = "委员  方向  权重\n张三  看多   1.5";
    const std::string html = pre_bubble("投票", raw, "assistant");
    FP_CHECK(html.find("<pre") != std::string::npos);
    FP_CHECK(html.find("</pre>") != std::string::npos);
    FP_CHECK_EQ(count(html, "<br/>"), std::size_t(0));
    FP_CHECK_EQ(count(html, "\n"), std::size_t(1));      // 原样的那一个换行
    // 等宽字体族必须写在**内联样式**里：只靠 <style> 太脆，
    // 退化成比例字体的话整张表就散了。
    FP_CHECK(html.find("monospace") != std::string::npos);
}

FP_TEST(chatrender, "等宽气泡里的标签符号也转义") {
    const std::string html = pre_bubble("事件", "error: expected <eof>", "error");
    FP_CHECK(html.find("&lt;eof&gt;") != std::string::npos);
}

FP_TEST(chatrender, "页头提供样式且不改动内容") {
    const std::string head = page_head();
    FP_CHECK(head.find("<style>") != std::string::npos);
    FP_CHECK(head.find("</style>") != std::string::npos);
    // 页头会被拼在累计 HTML 的最前面，它自己不能带未闭合的标签结构。
    FP_CHECK_EQ(count(head, "<style>"), count(head, "</style>"));
}
