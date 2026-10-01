// FinPulse Terminal — 图表的拖动模式状态机
//
// **为什么单独抽出来**：这个交互逻辑不依赖 Qt，抽出来就能被单元测试覆盖。
// 它值得测，是因为它曾经错得很隐蔽 ——
//
//   旧实现在 mousePressEvent 里无条件把 `drag_active_` 置为 true，而唯一
//   把它复位的地方是 leaveEvent。于是**单击一次之后图表就永久处于拖动
//   状态**：鼠标一动 K 线就跑，直到用户把指针移出图表区域才恢复。
//   没有任何报错，也没有崩溃，只是"用起来手感不对"，靠跑一遍看不出来。
//
// 交互约定（用户明确要求的）：
//
//     点一下 → 进入拖动模式（此后移动鼠标即平移）
//     再点一下 → 退出拖动模式
//
// 判定"点击"而不是"拖动"的依据是**按下点到释放点之间的位移**：小于阈值
// 算点击。没有这个判定的话，"拖完松手"也会被当成点击，模式就乱了。
#pragma once

#include <cstdlib>

namespace fp::gui {

/// 平移拖动模式的状态机。纯逻辑，无 Qt、无 IO。
class DragToggle {
public:
    /// 位移不超过这个像素数就算"点击"而非"拖动"。
    /// 取 3 是因为手持鼠标点击时几乎必然会有一两像素抖动 ——
    /// 阈值设 0 会让正常的点击被判成拖动，模式永远切不动。
    static constexpr int kClickSlopPx = 3;

    /// 左键按下。
    void press(int x) noexcept {
        active_   = true;
        moved_    = false;
        press_x_  = x;
    }

    /// 指针移动。用于累积"是否已经构成拖动"。
    void move(int x) noexcept {
        if (!active_) return;
        if (std::abs(x - press_x_) > kClickSlopPx) moved_ = true;
    }

    /// 左键释放。返回 true 表示这是一次**点击**（调用方应据此切换拖动模式）。
    ///
    /// 注意返回值只在"本次确实按下过"时有意义：没有按下就收到释放
    /// （例如别处按住、移进控件再松开）不算点击，不能翻转模式。
    bool release(int x) noexcept {
        if (!active_) return false;
        // 顺序要紧：先把释放点算作一次移动，**再**清 active_。
        // 反过来的话 move() 会因为 active_ 已为假而直接返回，释放点的
        // 位移就白算了 —— 表现是"快速甩动后松手"被误判成点击，模式乱翻。
        // （这个顺序问题是被 test_只有释放点离得远也算拖动 抓出来的。）
        move(x);
        active_ = false;
        return !moved_;
    }

    /// 是否正处于"按下未松开"的状态。
    bool active() const noexcept { return active_; }

    /// 当前按下点相对某点的位移，用于平移量的计算。
    int delta(int x) const noexcept { return x - press_x_; }

    /// 按下点的 x 坐标。
    int press_x() const noexcept { return press_x_; }

private:
    bool active_{false};
    bool moved_{false};
    int  press_x_{0};
};

}  // namespace fp::gui
