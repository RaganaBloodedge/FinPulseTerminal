// FinPulse Terminal — 图表拖动模式状态机测试
//
// 这组测试对应一个真实的**手感缺陷**：
//
//   旧实现里"按下左键"直接等于"开始拖动"（`drag_active_ = true`），
//   而唯一把它复位的地方是 leaveEvent。于是单击一次之后，图表就永久
//   跟着鼠标跑了 —— 直到用户把指针移出图表区域才恢复。
//
//   它没有任何可观测的"错误"：不崩、不报错、日志干净，只是用起来不对。
//   这类缺陷靠跑一遍程序发现不了，只能把交互语义写成断言。
//
// 新语义（用户明确要求的）：**点一下进入拖动，再点一下退出**。
// 判定"这是点击而非拖动"的依据，是按下点与释放点之间的位移是否在
// 容差之内（见 DragToggle::kClickSlopPx）。

#include "TestFramework.h"

#include "gui/ChartInteraction.h"

using namespace fp::gui;

FP_TEST(chartinteraction, "按下与释放在同一处算点击") {
    DragToggle d;
    d.press(100);
    FP_CHECK(d.active());
    FP_CHECK(d.release(100));   // true = 这次是点击，调用方据此切换模式
    FP_CHECK(!d.active());
}

FP_TEST(chartinteraction, "阈值内的抖动仍然算点击") {
    // 手持鼠标点一下必然带一两个像素的抖动。阈值设成 0 的话，正常点击
    // 会被判成拖动，模式永远切不动 —— 这就是这个容差存在的理由，
    // 所以要有一条测试专门守着它，防止有人"顺手"把它改成 0。
    DragToggle d;
    d.press(100);
    d.move(102);
    FP_CHECK(d.release(101));
}

FP_TEST(chartinteraction, "移动超过阈值算拖动而不是点击") {
    DragToggle d;
    d.press(100);
    d.move(140);
    FP_CHECK(!d.release(140));
}

FP_TEST(chartinteraction, "只有释放点离得远也算拖动") {
    // 中间没有 move 事件、直接来一次远距离释放（快速甩动）时，
    // release 自己也要做一次位移判定，不能只看 move 有没有被调用过。
    DragToggle d;
    d.press(100);
    FP_CHECK(!d.release(160));
}

FP_TEST(chartinteraction, "没有按下过就释放不算点击") {
    // 在别处按住、指针移进图表再松开。若把它当成点击，模式会被莫名其妙
    // 地翻转一次 —— 用户会觉得"我没点它啊"。
    DragToggle d;
    FP_CHECK(!d.release(50));
}

FP_TEST(chartinteraction, "位移量相对按下点计算") {
    DragToggle d;
    d.press(120);
    FP_CHECK_EQ(d.delta(150), 30);
    FP_CHECK_EQ(d.delta(90), -30);
    FP_CHECK_EQ(d.press_x(), 120);
}

FP_TEST(chartinteraction, "拖完回到原处松手不误判成点击") {
    // 拖出去又拖回来，松手时离按下点很近 —— 但整个过程显然是拖动。
    // moved_ 一旦置位就不再回落，正是为了这种情况。
    DragToggle d;
    d.press(10);
    d.move(60);
    d.move(20);
    FP_CHECK(!d.release(20));
}

FP_TEST(chartinteraction, "两次点击各自都算点击") {
    // 用户要的语义：点一下进入拖动，再点一下取消拖动。
    // 两次都必须是"点击"，否则第二次取消不掉。
    DragToggle d;
    d.press(10);
    const bool first = d.release(10);
    d.press(20);
    const bool second = d.release(20);
    FP_CHECK(first);
    FP_CHECK(second);
}

FP_TEST(chartinteraction, "一次按下只产生一次判定") {
    // 释放两次（例如重复投递的合成事件）不能产生两次切换。
    DragToggle d;
    d.press(10);
    FP_CHECK(d.release(10));
    FP_CHECK(!d.release(10));
}
