// FinPulse Terminal — 自绘 K 线图
//
// 为什么不用 QtCharts 或者 QCustomPlot：
//   1. K 线（蜡烛）不是任何一种现成图表类型，用第三方库同样要自己写
//      drawSeries 或者用一堆 QGraphicsItem 拼，代码量不见得更少；
//   2. 多一套依赖就多一份发布体积和版本兼容风险；
//   3. 自绘让坐标映射完全可控 —— 十字光标、缩放、叠加线、预测区间
//      这些交互实现起来反而更直接。
//   代价是坐标轴、网格、刻度这些都要自己写，大约多出 150 行。
//
// 颜色遵循中国市场习惯：涨红跌绿。
#pragma once

#include <QColor>
#include <QString>
#include <QVector>
#include <QWidget>

#include "ChartInteraction.h"
#include "model/Types.h"

namespace fp::gui {

class CandleChartWidget : public QWidget {
    Q_OBJECT

public:
    explicit CandleChartWidget(QWidget* parent = nullptr);

    void setSeries(const fp::CandleSeries& series);
    void setTitle(const QString& title);

    /// 进入流式模式：清空 K 线、叠加线与预测，等着 appendBar 逐根喂进来。
    /// 回放行情走的就是这条路 —— 和接真实行情时是同一条代码路径。
    void beginStreaming();

    /// 追加一根 K 线。
    /// 与 setSeries 的区别：不清空、不重置视图。序列短于一屏时窗口跟着长大，
    /// 铺满后变成滚动的实时窗口；用户手动缩放/平移过的视图不会被拽回去。
    void appendBar(const fp::Candle& bar);

    /// 叠加一条线（均线 / 布林带 / 信号线…）。
    /// values 与 K 线等长，NaN 表示该点无值（前导不足的位置）。
    void setOverlay(const QString& name, const QVector<double>& values,
                    const QColor& color, bool dashed = false);
    void clearOverlays();

    void setForecast(const QVector<fp::ForecastPoint>& points);
    void clearForecast();

    void setShowVolume(bool on);
    void setShowGrid(bool on);
    void resetView();

    /// 视图里当前显示多少根 K 线。
    int visibleBars() const { return visible_bars_; }

    /// 平移模式当前是否开启。
    ///
    /// 交互约定：**点一下进入拖动，再点一下退出**（不是"按住才拖"）。
    /// 之所以做成模式，是因为平移是这个图表的高频操作，按住拖动在触控板
    /// 和触屏上很难维持；而做成模式就必须能**明确看出当前是否处于模式中**,
    /// 所以光标会随之变化（小手 / 箭头）。
    bool dragMode() const { return drag_mode_; }
    void setDragMode(bool on);

signals:
    /// 鼠标停在某根 K 线上时发出（离开则 index = -1）。
    void hoveredIndexChanged(int index);

    /// 平移模式切换时发出，供状态栏给出文字提示。
    void dragModeChanged(bool on);

protected:
    void paintEvent(QPaintEvent* event) override;
    void wheelEvent(QWheelEvent* event) override;
    void mouseMoveEvent(QMouseEvent* event) override;
    void mousePressEvent(QMouseEvent* event) override;
    void mouseReleaseEvent(QMouseEvent* event) override;
    void leaveEvent(QEvent* event) override;

private:
    struct Overlay {
        QString         name;
        QVector<double> values;
        QColor          color;
        bool            dashed{false};
    };

    // 布局
    QRect  plotRect() const;
    QRect  volumeRect() const;
    QRect  axisRect() const;

    // 坐标映射
    int    totalSlots() const;              ///< K 线 + 预测点
    double priceToY(double price) const;
    int    slotToX(int slot) const;
    int    xToSlot(int x) const;
    void   recomputeScale();

    /// 按当前平移模式刷新鼠标光标（模式开=小手，关=箭头）。
    void   syncCursor();

    void   drawGrid(QPainter& p);
    void   drawCandles(QPainter& p);
    void   drawVolume(QPainter& p);
    void   drawOverlays(QPainter& p);
    void   drawForecast(QPainter& p);
    void   drawAxes(QPainter& p);
    void   drawCrosshair(QPainter& p);
    void   drawLegend(QPainter& p);

    fp::CandleSeries           series_;
    QVector<Overlay>           overlays_;
    QVector<fp::ForecastPoint> forecast_;

    int    visible_bars_{120};
    int    offset_{0};        ///< 视图右端相对最后一根 K 线的偏移
    double price_min_{0.0};
    double price_max_{1.0};
    long long volume_max_{1};
    QString  title_;
    bool     show_volume_{true};
    bool     show_grid_{true};
    int      hover_slot_{-1};

    /// 按下/移动/释放的判定（纯逻辑，见 ChartInteraction.h）。
    DragToggle drag_;

    /// 平移模式是否开启。由"点击"切换，不随鼠标离开复位 ——
    /// 它是用户显式选择的模式，不是一次瞬时动作。
    bool drag_mode_{false};

    /// 本次拖动开始时的视图偏移，平移量以它为基准累加。
    int drag_start_offset_{0};
};

}  // namespace fp::gui
