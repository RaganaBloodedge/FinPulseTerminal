// FinPulse Terminal — 对话记录的 HTML 渲染
//
// 全是**纯函数**：输入一段文本，输出一段 HTML。没有 Qt、没有状态、没有 IO。
//
// 为什么提到头文件、还做成纯函数：把渲染和界面写在一起，这一层就永远
// 测不到 —— 而它恰恰容易出两类问题：
//   * 转义漏了：角色报告里出现一个 `<` 就把后面整段吞掉（Qt 的富文本
//     解析器会把它当地标签）。报告里出现 `<` 不是罕见情况（"< 60 根"
//     "RSI > 70 < 80"），所以转义必须是无条件的第一步；
//   * 气泡结构拼错：少一个 </td> 会让后面所有消息挤在同一格里，
//     肉眼看起来像"消息丢了"。
#pragma once

#include <string>
#include <vector>

namespace fp {
namespace chat {

/// HTML 转义。**任何用户/模型产出的文本进 HTML 之前都必须过这一道。**
inline std::string escape(const std::string& text) {
    std::string out;
    out.reserve(text.size() + text.size() / 8);
    for (char c : text) {
        switch (c) {
            case '&': out += "&amp;"; break;
            case '<': out += "&lt;"; break;
            case '>': out += "&gt;"; break;
            case '"': out += "&quot;"; break;
            case '\'': out += "&#39;"; break;
            default: out.push_back(c);
        }
    }
    return out;
}

/// 把换行变成 <br/>。转义之后再做，顺序不能反 ——
/// 反过来会把我们自己插入的 <br/> 也转义掉。
inline std::string with_breaks(const std::string& escaped) {
    std::string out;
    out.reserve(escaped.size() + 16);
    for (char c : escaped) {
        if (c == '\n') out += "<br/>";
        else out.push_back(c);
    }
    return out;
}

/// 轻量 Markdown：只认模型最常用的三种写法。
///
/// 刻意不做完整解析：`**粗体**`、行首 `#`（标题）、行首 `-`/`*`（列表项）。
/// 其余一律当纯文本 —— 一个半吊子解析器把 `2*3*4` 吃掉比不解析更糟。
inline std::string render_text(const std::string& raw) {
    std::vector<std::string> lines;
    std::string cur;
    for (char c : raw) {
        if (c == '\n') { lines.push_back(cur); cur.clear(); }
        else cur.push_back(c);
    }
    lines.push_back(cur);

    std::string out;
    for (std::size_t i = 0; i < lines.size(); ++i) {
        std::string line = lines[i];

        bool heading = false;
        std::size_t hash = 0;
        while (hash < line.size() && line[hash] == '#' && hash < 4) ++hash;
        if (hash > 0 && hash < line.size() && line[hash] == ' ') {
            line = line.substr(hash + 1);
            heading = true;
        }

        bool bullet = false;
        if (line.size() > 2 && (line[0] == '-' || line[0] == '*') && line[1] == ' ') {
            line = line.substr(2);
            bullet = true;
        }

        std::string body = escape(line);

        // **粗体** → <b>。只看成对的 **，落单的星号原样留着。
        std::string bolded;
        std::size_t pos = 0;
        while (true) {
            const std::size_t open = body.find("**", pos);
            if (open == std::string::npos) { bolded += body.substr(pos); break; }
            const std::size_t close = body.find("**", open + 2);
            if (close == std::string::npos) { bolded += body.substr(pos); break; }
            bolded += body.substr(pos, open - pos);
            bolded += "<b>" + body.substr(open + 2, close - open - 2) + "</b>";
            pos = close + 2;
        }

        if (heading) out += "<div><b>" + bolded + "</b></div>";
        else if (bullet) out += "<div>&nbsp;&nbsp;• " + bolded + "</div>";
        else out += "<div>" + (bolded.empty() ? std::string("&nbsp;") : bolded) + "</div>";
    }
    return out;
}

/// 整份记录区的页头样式。
///
/// Qt 的富文本只支持 CSS 的一个子集，这里刻意只用最基本的几项；`<pre>`
/// 上另外还挂了一份内联样式（见 :func:`pre_bubble`），因为不同平台样式下
/// `<pre>` 的默认字体族不一定是等宽的 —— 靠 `<style>` 单点保证太脆。
inline std::string page_head() {
    return "<style>"
           "body { font-size: 13px; }"
           "div  { margin: 0; }"
           "b    { font-weight: 600; }"
           "</style>";
}

/// 一条消息的气泡。用 table 而不是 div + CSS：Qt 的富文本只支持 CSS 的一个
/// 子集，`div` 上的 background/border 时灵时不灵，而 `td` 的 bgcolor 是
/// 稳的 —— 气泡这种东西，宁可丑一点也要每次都画出来。
///
/// ``tone``：user / assistant / system / error。
inline std::string bubble(const std::string& who, const std::string& body_html,
                          const std::string& tone) {
    std::string bg = "#f3f4f6", border = "#9ca3af";
    if (tone == "user")            { bg = "#eef4ff"; border = "#4a7fd1"; }
    else if (tone == "assistant")  { bg = "#f7fbf7"; border = "#3f9a5f"; }
    else if (tone == "error")      { bg = "#fff4f4"; border = "#c05050"; }

    std::string out;
    out += "<table width=\"100%\" cellspacing=\"0\" cellpadding=\"6\" "
           "style=\"margin-top:2px;margin-bottom:8px;\">";
    out += "<tr><td bgcolor=\"" + bg + "\" style=\"border-left:3px solid " + border + ";\">";
    out += "<div><b>" + escape(who) + "</b></div>";
    out += body_html;
    out += "</td></tr></table>";
    return out;
}

/// 等宽消息：投票表、事件流这类**靠空格对齐**的内容。
/// 走 Markdown 渲染会把它压扁，所以单独一条路径。
///
/// 字体族写在内联样式里而不是只靠 `<style>`：pre 的等宽是对齐的前提，
/// 退化成比例字体的话整张投票表就散了 —— 这条比"好不好看"重要。
inline std::string pre_bubble(const std::string& who, const std::string& raw,
                              const std::string& tone) {
    return bubble(who,
                  "<pre style=\"margin:0;white-space:pre-wrap;"
                  "font-family:Consolas,Menlo,'DejaVu Sans Mono',monospace;"
                  "font-size:12px;\">" + escape(raw) + "</pre>",
                  tone);
}

}  // namespace chat
}  // namespace fp
