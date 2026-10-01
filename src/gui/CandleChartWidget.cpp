#include "CandleChartWidget.h"

#include <QFontMetrics>
#include <QMouseEvent>
#include <QPainter>
#include <QPainterPath>
#include <QWheelEvent>

#include <algorithm>
#include <cmath>
#include <limits>

namespace fp::gui {

namespace {

// 中国市场惯例：涨红跌绿（与欧美相反）。
// 这个选择和看盘软件保持一致，用户不需要在大脑里做一次颜色反转。
const QColor kUp(0xD1, 0x2B, 0x2B);
const QColor kDown(0x1E, 0x8E, 0x3E);
const QColor kBackground(0xFA, 0xFA, 0xFB);
const QColor kGrid(0xE6, 0xE6, 0xEA);
const QColor kAxis(0x9A, 0x9A, 0xA2);
const QColor kText(0x33, 0x33, 0x38);
const QColor kMuted(0x80, 0x80, 0x88);
const QColor kCrosshair(0x55, 0x66, 0x88);
const QColor kForecast(0x7C, 0x4D, 0xC4);

constexpr int kMarginTop     = 8;
constexpr int kMarginRight   = 62;
constexpr int kMarginBottom  = 20;
constexpr int kMarginLeft    = 6;
constexpr int kLegendHeight  = 20;

/// 流式模式（回放/实时行情）的可见窗口：序列短于一屏时跟着长，
/// 铺满后就固定成滚动的实时窗口。120 根是按日线看形态的经验值。
constexpr int kStreamingWindow = 120;
constexpr int kStreamingMin    = 20;

/// 视图左端对应第几个 slot。
int view_start(int total, int visible, int offset) {
    if (total <= 0) return 0;
    const int start = total - visible - offset;
    return std::clamp(start, 0, std::max(0, total - 1));
}

QString fmt_price(double v, int decimals = 2) {
    return QString::number(v, 'f', decimals);
}

}  // namespace

CandleChartWidget::CandleChartWidget(QWidget* parent) : QWidget(parent) {
    setMinimumSize(360, 260);
    setMouseTracking(true);
    setAutoFillBackground(true);
    setFocusPolicy(Qt::StrongFocus);

    QPalette pal = palette();
    pal.setColor(QPalette::Window, kBackground);
    setPalette(pal);
}

// ── 数据入口 ──────────────────────────────────────────────

void CandleChartWidget::setSeries(const fp::CandleSeries& series) {
    series_ = series;
    offset_ = 0;
    hover_slot_ = -1;
    // 视图默认展示最后 120 根。太少看不出形态，太多蜡烛会细成一根线。
    visible_bars_ = std::clamp(static_cast<int>(series_.size()), 20, 160);
    recomputeScale();
    update();
}

void CandleChartWidget::appendBar(const fp::Candle& bar) {
    series_.push(bar);

    const int n = static_cast<int>(series_.size());
    // 序列还没铺满一屏时让窗口跟着长大；铺满之后就不再改窗口宽度，
    // 于是新 K 线把最老的挤出左边界 —— 这才是"实时窗口"该有的行为。
    if (n <= kStreamingWindow) {
        visible_bars_ = std::clamp(n, kStreamingMin, kStreamingWindow);
    }

    // 刻意不动 offset_：用户如果手动缩放/平移过，视图就留在原处。
    // 每来一根新 K 线就把画面拽回右端，是很多行情软件让人恼火的地方。
    recomputeScale();
    update();
}

void CandleChartWidget::beginStreaming() {
    series_   = fp::CandleSeries{};
    overlays_.clear();
    forecast_.clear();
    offset_       = 0;
    visible_bars_ = kStreamingMin;
    hover_slot_   = -1;
    recomputeScale();
    update();
}

void CandleChartWidget::setTitle(const QString& title) {
    title_ = title;
    update();
}

void CandleChartWidget::setOverlay(const QString& name, const QVector<double>& values,
                                   const QColor& color, bool dashed) {
    for (auto& ov : overlays_) {
        if (ov.name == name) {
            ov.values = values;
            ov.color  = color;
            ov.dashed = dashed;
            recomputeScale();
            update();
            return;
        }
    }
    overlays_.append(Overlay{name, values, color, dashed});
    recomputeScale();
    update();
}

void CandleChartWidget::clearOverlays() {
    overlays_.clear();
    recomputeScale();
    update();
}

void CandleChartWidget::setForecast(const QVector<fp::ForecastPoint>& points) {
    forecast_ = points;
    recomputeScale();
    update();
}

void CandleChartWidget::clearForecast() {
    forecast_.clear();
    recomputeScale();
    update();
}

void CandleChartWidget::setShowVolume(bool on) {
    show_volume_ = on;
    recomputeScale();
    update();
}

void CandleChartWidget::setShowGrid(bool on) {
    show_grid_ = on;
    update();
}

void CandleChartWidget::resetView() {
    offset_ = 0;
    visible_bars_ = std::clamp(static_cast<int>(series_.size()), 20, 160);
    recomputeScale();
    update();
}

// ── 布局与映射 ────────────────────────────────────────────

QRect CandleChartWidget::plotRect() const {
    const int top = kMarginTop + kLegendHeight;
    const int volume_h = show_volume_ ? std::max(36, height() / 5) : 0;
    const int h = std::max(40, height() - top - kMarginBottom - volume_h - (show_volume_ ? 4 : 0));
    const int w = std::max(40, width() - kMarginLeft - kMarginRight);
    return QRect(kMarginLeft, top, w, h);
}

QRect CandleChartWidget::volumeRect() const {
    if (!show_volume_) return {};
    const QRect pr = plotRect();
    const int top = pr.bottom() + 5;
    const int h = std::max(12, height() - kMarginBottom - top);
    return QRect(pr.left(), top, pr.width(), h);
}

QRect CandleChartWidget::axisRect() const {
    const QRect pr = plotRect();
    return QRect(pr.right() + 1, pr.top(), kMarginRight - 1, pr.height());
}

int CandleChartWidget::totalSlots() const {
    return static_cast<int>(series_.size()) + static_cast<int>(forecast_.size());
}

double CandleChartWidget::priceToY(double price) const {
    const QRect pr = plotRect();
    const double span = std::max(1e-9, price_max_ - price_min_);
    const double ratio = (price - price_min_) / span;
    return pr.bottom() - ratio * pr.height();
}

int CandleChartWidget::slotToX(int slot) const {
    const QRect  pr    = plotRect();
    const int    total = totalSlots();
    if (total <= 0) return pr.left();

    const int start = view_start(total, visible_bars_, offset_);
    const double slot_w = static_cast<double>(pr.width()) / std::max(1, visible_bars_);
    return pr.left() + static_cast<int>((slot - start) * slot_w);
}

int CandleChartWidget::xToSlot(int x) const {
    const QRect  pr    = plotRect();
    const int    total = totalSlots();
    if (total <= 0) return -1;

    const int    start  = view_start(total, visible_bars_, offset_);
    const double slot_w = static_cast<double>(pr.width()) / std::max(1, visible_bars_);
    if (slot_w <= 0.0) return -1;

    const int slot = start + static_cast<int>((x - pr.left()) / slot_w);
    return (slot >= 0 && slot < total) ? slot : -1;
}

void CandleChartWidget::recomputeScale() {
    const int total = totalSlots();
    if (total <= 0) {
        price_min_  = 0.0;
        price_max_  = 1.0;
        volume_max_ = 1;
        return;
    }

    const int bars  = static_cast<int>(series_.size());
    const int start = view_start(total, visible_bars_, offset_);
    const int end   = std::min(total, start + visible_bars_);

    double    lo = std::numeric_limits<double>::max();
    double    hi = std::numeric_limits<double>::lowest();
    long long vmax = 0;

    for (int slot = start; slot < end; ++slot) {
        if (slot < bars) {
            const auto& b = series_.at(static_cast<std::size_t>(slot));
            lo = std::min(lo, b.low);
            hi = std::max(hi, b.high);
            vmax = std::max(vmax, static_cast<long long>(b.volume));
        }
        for (const auto& ov : overlays_) {
            const int n = static_cast<int>(ov.values.size());
            if (slot < n) {
                const double v = ov.values[slot];
                if (std::isfinite(v)) {
                    lo = std::min(lo, v);
                    hi = std::max(hi, v);
                }
            }
        }
        const int fi = slot - bars;
        const int nf = static_cast<int>(forecast_.size());
        if (fi >= 0 && fi < nf) {
            lo = std::min(lo, forecast_[fi].lower);
            hi = std::max(hi, forecast_[fi].upper);
        }
    }

    if (!(lo <= hi)) {  // 同时覆盖 lo/hi 未被赋值的 NaN 情况
        lo = 0.0;
        hi = 1.0;
    }
    if (hi - lo < 1e-9) hi = lo + 1.0;

    const double pad = (hi - lo) * 0.06;
    price_min_  = lo - pad;
    price_max_  = hi + pad;
    volume_max_ = std::max<long long>(1, vmax);
}

// ── 绘制 ──────────────────────────────────────────────────

void CandleChartWidget::paintEvent(QPaintEvent*) {
    QPainter p(this);
    p.setRenderHint(QPainter::Antialiasing, true);
    p.setRenderHint(QPainter::TextAntialiasing, true);

    p.fillRect(rect(), kBackground);

    if (series_.empty()) {
        p.setPen(kMuted);
        QFont f = p.font();
        f.setPointSizeF(f.pointSizeF() + 1.0);
        p.setFont(f);
        p.drawText(rect(), Qt::AlignCenter,
                   QStringLiteral("暂无数据\n\n在工具栏选择数据源与标的，然后点「重新分析」"));
        return;
    }

    drawGrid(p);
    drawVolume(p);
    drawCandles(p);
    drawOverlays(p);
    drawForecast(p);
    drawAxes(p);
    drawLegend(p);
    drawCrosshair(p);
}

void CandleChartWidget::drawGrid(QPainter& p) {
    if (!show_grid_) return;

    const QRect pr = plotRect();
    p.setPen(QPen(kGrid, 1, Qt::SolidLine));

    // 水平网格：5 等分
    for (int i = 0; i <= 4; ++i) {
        const int y = pr.top() + pr.height() * i / 4;
        p.drawLine(pr.left(), y, pr.right(), y);
    }

    // 垂直网格：最多 8 条，避免数据少时糊成一片
    const int total = totalSlots();
    const int start = view_start(total, visible_bars_, offset_);
    const int step  = std::max(1, visible_bars_ / 8);
    for (int slot = start; slot < start + visible_bars_; slot += step) {
        const double slot_w = static_cast<double>(pr.width()) / std::max(1, visible_bars_);
        const int    x = pr.left() + static_cast<int>((slot - start) * slot_w);
        p.drawLine(x, pr.top(), x, pr.bottom());
    }
}

void CandleChartWidget::drawCandles(QPainter& p) {
    const QRect pr    = plotRect();
    const int   total = totalSlots();
    const int   bars  = static_cast<int>(series_.size());
    const int   start = view_start(total, visible_bars_, offset_);
    const int   end   = std::min(bars, start + visible_bars_);

    const double slot_w = static_cast<double>(pr.width()) / std::max(1, visible_bars_);
    const double body_w = std::max(1.0, slot_w * 0.68);
    const bool   tiny   = body_w < 3.0;  // 太窄时空心框会糊掉，改用实心

    for (int slot = start; slot < end; ++slot) {
        const auto&  b     = series_.at(static_cast<std::size_t>(slot));
        const double cx    = pr.left() + (slot - start) * slot_w + slot_w / 2.0;
        const QColor color = (b.close >= b.open) ? kUp : kDown;
        const bool   rising = b.close >= b.open;

        // 上下影线
        p.setPen(QPen(color, 1));
        p.drawLine(QPointF(cx, priceToY(b.high)), QPointF(cx, priceToY(b.low)));

        // 实体
        const double y_open  = priceToY(b.open);
        const double y_close = priceToY(b.close);
        const double top     = std::min(y_open, y_close);
        const double height  = std::max(1.0, std::fabs(y_close - y_open));

        const QRectF body(cx - body_w / 2.0, top, body_w, height);
        if (rising && !tiny) {
            // 阳线空心 —— 与国内看盘软件一致
            p.setBrush(Qt::NoBrush);
            p.setPen(QPen(color, 1));
            p.drawRect(body);
        } else {
            p.setBrush(color);
            p.setPen(Qt::NoPen);
            p.drawRect(body);
        }
    }
}

void CandleChartWidget::drawVolume(QPainter& p) {
    if (!show_volume_) return;

    const QRect vr    = volumeRect();
    const QRect pr    = plotRect();
    const int   total = totalSlots();
    const int   bars  = static_cast<int>(series_.size());
    const int   start = view_start(total, visible_bars_, offset_);
    const int   end   = std::min(bars, start + visible_bars_);

    p.setPen(QPen(kGrid, 1));
    p.drawLine(vr.left(), vr.bottom(), vr.right(), vr.bottom());

    const double slot_w = static_cast<double>(pr.width()) / std::max(1, visible_bars_);
    const double body_w = std::max(1.0, slot_w * 0.68);

    for (int slot = start; slot < end; ++slot) {
        const auto&  b  = series_.at(static_cast<std::size_t>(slot));
        const double cx = pr.left() + (slot - start) * slot_w + slot_w / 2.0;
        const double h  = vr.height() * (static_cast<double>(b.volume) /
                                         static_cast<double>(std::max<long long>(1, volume_max_)));

        const bool   rising = b.close >= b.open;
        QColor       color  = rising ? kUp : kDown;
        color.setAlpha(150);

        p.setPen(Qt::NoPen);
        p.setBrush(color);
        p.drawRect(QRectF(cx - body_w / 2.0, vr.bottom() - h, body_w, std::max(1.0, h)));
    }
}

void CandleChartWidget::drawOverlays(QPainter& p) {
    const QRect pr    = plotRect();
    const int   total = totalSlots();
    const int   start = view_start(total, visible_bars_, offset_);
    const int   end   = std::min(total, start + visible_bars_);

    for (const auto& ov : overlays_) {
        const int n = static_cast<int>(ov.values.size());
        if (n == 0) continue;

        QPainterPath path;
        bool         started = false;

        for (int slot = start; slot < end; ++slot) {
            if (slot >= n) break;
            const double v = ov.values[slot];
            if (!std::isfinite(v)) {
                started = false;  // 断开，别把前导缺失区和有效区连成一条直线
                continue;
            }
            const double x = slotToX(slot) +
                             (static_cast<double>(pr.width()) / std::max(1, visible_bars_)) / 2.0;
            const double y = priceToY(v);
            if (!started) {
                path.moveTo(x, y);
                started = true;
            } else {
                path.lineTo(x, y);
            }
        }

        QPen pen(ov.color, 1.6);
        if (ov.dashed) pen.setStyle(Qt::DashLine);
        p.setPen(pen);
        p.setBrush(Qt::NoBrush);
        p.drawPath(path);
    }
}

void CandleChartWidget::drawForecast(QPainter& p) {
    if (forecast_.isEmpty() || series_.empty()) return;

    const QRect  pr         = plotRect();
    const int    total      = totalSlots();
    const int    bars       = static_cast<int>(series_.size());
    const double slot_w     = static_cast<double>(pr.width()) / std::max(1, visible_bars_);

    // 置信区间用半透明色块
    QPainterPath band;
    bool         started = false;
    for (int i = 0; i < forecast_.size(); ++i) {
        const int    slot = bars + i;
        const double cx   = pr.left() +
                            (slot - view_start(total, visible_bars_, offset_)) * slot_w + slot_w / 2.0;
        const double y_hi = priceToY(forecast_[i].upper);
        const double y_lo = priceToY(forecast_[i].lower);
        if (!started) {
            band.moveTo(cx, y_hi);
            started = true;
        } else {
            band.lineTo(cx, y_hi);
        }
        band.lineTo(cx, y_lo);
    }
    if (started) {
        // 回程把下沿连回去，形成一个闭合带
        for (int i = static_cast<int>(forecast_.size()) - 1; i >= 0; --i) {
            const int    slot = bars + i;
            const double cx   = pr.left() +
                                (slot - view_start(total, visible_bars_, offset_)) * slot_w + slot_w / 2.0;
            band.lineTo(cx, priceToY(forecast_[i].lower));
        }
        QColor fill = kForecast;
        fill.setAlpha(38);
        p.setPen(Qt::NoPen);
        p.setBrush(fill);
        p.drawPath(band);
    }

    // 预测中位线：从最后一根收盘价连出去
    QPainterPath line;
    const double last_x = pr.left() +
                          (bars - 1 - view_start(total, visible_bars_, offset_)) * slot_w +
                          slot_w / 2.0;
    line.moveTo(last_x, priceToY(series_.back().close));
    for (int i = 0; i < forecast_.size(); ++i) {
        const int    slot = bars + i;
        const double cx   = pr.left() +
                            (slot - view_start(total, visible_bars_, offset_)) * slot_w + slot_w / 2.0;
        line.lineTo(cx, priceToY(forecast_[i].value));
    }

    QPen pen(kForecast, 1.8);
    pen.setStyle(Qt::DashLine);
    p.setPen(pen);
    p.setBrush(Qt::NoBrush);
    p.drawPath(line);

    // 预测点用小圆点标出来
    p.setBrush(kForecast);
    p.setPen(Qt::NoPen);
    for (int i = 0; i < forecast_.size(); ++i) {
        const int    slot = bars + i;
        const double cx   = pr.left() +
                            (slot - view_start(total, visible_bars_, offset_)) * slot_w + slot_w / 2.0;
        p.drawEllipse(QPointF(cx, priceToY(forecast_[i].value)), 2.2, 2.2);
    }
}

void CandleChartWidget::drawAxes(QPainter& p) {
    const QRect pr = plotRect();

    QFont f = p.font();
    f.setPointSizeF(std::max(7.5, f.pointSizeF() - 1.0));
    p.setFont(f);
    const QFontMetrics fm(f);

    // ── 右侧价格轴 ──
    p.setPen(kAxis);
    p.drawLine(pr.right(), pr.top(), pr.right(), pr.bottom());

    const int decimals = (price_max_ < 10.0) ? 4 : 2;
    for (int i = 0; i <= 4; ++i) {
        const double ratio = 1.0 - i / 4.0;
        const double price = price_min_ + (price_max_ - price_min_) * ratio;
        const int    y     = pr.top() + pr.height() * i / 4;
        const QString text = fmt_price(price, decimals);

        p.setPen(kGrid);
        p.drawLine(pr.right(), y, pr.right() + 4, y);
        p.setPen(kText);
        p.drawText(QRect(pr.right() + 6, y - fm.height() / 2, kMarginRight - 8, fm.height()),
                   Qt::AlignLeft | Qt::AlignVCenter, text);
    }

    // ── 底部日期轴 ──
    const int total = totalSlots();
    const int bars  = static_cast<int>(series_.size());
    const int start = view_start(total, visible_bars_, offset_);
    const int step  = std::max(1, visible_bars_ / 5);
    const double slot_w = static_cast<double>(pr.width()) / std::max(1, visible_bars_);

    const int axis_y = show_volume_ ? volumeRect().bottom() + 2 : pr.bottom() + 2;

    for (int slot = start; slot < start + visible_bars_; slot += step) {
        if (slot >= bars) break;
        const int     x = pr.left() + static_cast<int>((slot - start) * slot_w) +
                          static_cast<int>(slot_w / 2.0);
        const QString text = QString::fromStdString(format_date(series_.at(
                                 static_cast<std::size_t>(slot)).ts_ms));

        p.setPen(kAxis);
        p.drawLine(x, axis_y, x, axis_y + 3);
        p.setPen(kText);
        p.drawText(QRect(x - 34, axis_y + 3, 68, kMarginBottom - 4),
                   Qt::AlignHCenter | Qt::AlignTop, text);
    }
}

void CandleChartWidget::drawLegend(QPainter& p) {
    QFont f = p.font();
    f.setPointSizeF(std::max(7.5, f.pointSizeF() - 1.0));
    p.setFont(f);

    const QFontMetrics fm(f);
    int x = kMarginLeft + 2;
    const int y = kMarginTop + 2;

    if (!title_.isEmpty()) {
        QFont bold = f;
        bold.setBold(true);
        p.setFont(bold);
        p.setPen(kText);
        p.drawText(x, y + fm.ascent(), title_);
        x += QFontMetrics(bold).horizontalAdvance(title_) + 14;
        p.setFont(f);
    }

    // 每条叠加线显示名称 + 最后一个有效值 —— 和看盘软件的习惯一致
    for (const auto& ov : overlays_) {
        const int n = static_cast<int>(ov.values.size());
        double last = std::numeric_limits<double>::quiet_NaN();
        for (int i = n - 1; i >= 0; --i) {
            if (std::isfinite(ov.values[i])) {
                last = ov.values[i];
                break;
            }
        }
        if (!std::isfinite(last)) continue;

        const QString text = QStringLiteral("%1 %2").arg(ov.name, fmt_price(last, 2));
        const int     w    = fm.horizontalAdvance(text);
        if (x + w > width() - kMarginRight) break;

        p.setPen(QPen(ov.color, 2));
        p.drawLine(x, y + fm.ascent() - 4, x + 10, y + fm.ascent() - 4);
        p.setPen(kText);
        p.drawText(x + 14, y + fm.ascent(), text);
        x += w + 22;
    }
}

void CandleChartWidget::drawCrosshair(QPainter& p) {
    if (hover_slot_ < 0) return;

    const QRect pr    = plotRect();
    const int   total = totalSlots();
    const double slot_w = static_cast<double>(pr.width()) / std::max(1, visible_bars_);

    const int start = view_start(total, visible_bars_, offset_);
    const int x     = pr.left() + static_cast<int>((hover_slot_ - start) * slot_w) +
                      static_cast<int>(slot_w / 2.0);
    if (x < pr.left() || x > pr.right()) return;

    p.setPen(QPen(kCrosshair, 1, Qt::DashLine));
    p.drawLine(x, pr.top(), x, pr.bottom());

    // 底部日期提示
    const int bars = static_cast<int>(series_.size());
    const int axis_y = show_volume_ ? volumeRect().bottom() + 2 : pr.bottom() + 2;

    QString label;
    if (hover_slot_ < bars) {
        const auto& b = series_.at(static_cast<std::size_t>(hover_slot_));
        label = QStringLiteral("%1  开 %2  高 %3  低 %4  收 %5")
                    .arg(QString::fromStdString(format_date(b.ts_ms)),
                         fmt_price(b.open), fmt_price(b.high),
                         fmt_price(b.low), fmt_price(b.close));

        // 在鼠标所在价格处画横线
        const double y = priceToY(b.close);
        p.setPen(QPen(kCrosshair, 1, Qt::DashLine));
        p.drawLine(pr.left(), y, pr.right(), y);
        p.setPen(kCrosshair);
        p.drawLine(pr.right(), y, pr.right() + 4, y);
    } else {
        const int fi = hover_slot_ - bars;
        const int nf = static_cast<int>(forecast_.size());
        if (fi < nf) {
            const auto& fp = forecast_[fi];
            label = QStringLiteral("预测  %1   区间 [%2, %3]")
                        .arg(QString::fromStdString(format_date(fp.ts_ms)),
                             fmt_price(fp.lower), fmt_price(fp.upper));
        }
    }

    if (label.isEmpty()) return;

    QFont f = p.font();
    f.setPointSizeF(std::max(7.5, f.pointSizeF() - 1.0));
    p.setFont(f);
    const QFontMetrics fm(f);

    const int w = fm.horizontalAdvance(label) + 12;
    QRect     box(std::min(x + 8, width() - w - 4), axis_y - fm.height() - 6,
                  w, fm.height() + 4);

    p.setPen(Qt::NoPen);
    QColor bg = kCrosshair;
    bg.setAlpha(225);
    p.setBrush(bg);
    p.drawRoundedRect(box, 3, 3);
    p.setPen(Qt::white);
    p.drawText(box, Qt::AlignCenter, label);
}

// ── 交互 ──────────────────────────────────────────────────

void CandleChartWidget::wheelEvent(QWheelEvent* event) {
    const int delta = event->angleDelta().y();
    if (delta == 0) return;

    const int old_visible = visible_bars_;
    if (delta > 0) {
        visible_bars_ = std::max(20, static_cast<int>(visible_bars_ * 0.85));
    } else {
        visible_bars_ = std::min(600, static_cast<int>(visible_bars_ * 1.18));
    }
    if (visible_bars_ == old_visible) return;

    // 让光标位置对应的 K 线尽量留在原地，缩放才不会"跳"
    const int    cursor_x = event->position().x();
    const int    cursor_slot = xToSlot(cursor_x);
    const double pr_width = std::max(1, plotRect().width());
    const double ratio = std::clamp((cursor_x - plotRect().left()) / pr_width, 0.0, 1.0);

    if (cursor_slot >= 0) {
        const int total  = totalSlots();
        const int anchor = cursor_slot - static_cast<int>(ratio * visible_bars_);
        offset_ = std::clamp(total - visible_bars_ - anchor, 0, std::max(0, total - visible_bars_));
    }

    recomputeScale();
    update();
    event->accept();
}

void CandleChartWidget::setDragMode(bool on) {
    if (drag_mode_ == on) return;
    drag_mode_ = on;
    syncCursor();
    emit dragModeChanged(on);
}

void CandleChartWidget::syncCursor() {
    setCursor(drag_mode_ ? Qt::OpenHandCursor : Qt::ArrowCursor);
}

void CandleChartWidget::mouseMoveEvent(QMouseEvent* event) {
    const int x       = static_cast<int>(event->position().x());
    const int slot    = xToSlot(x);
    const int clamped = (slot >= 0 && slot < totalSlots()) ? slot : -1;

    drag_.move(x);

    // 平移的两个条件缺一不可：**模式已开启**，且**当时确实按着键**。
    // 只判模式不判按键的话，用户只是把鼠标移过图表，画面就会跟着跑。
    if (drag_mode_ && drag_.active()) {
        const double slot_w =
            static_cast<double>(plotRect().width()) / std::max(1, visible_bars_);
        if (slot_w > 0.0) {
            const int shift = static_cast<int>(drag_.delta(x) / slot_w);
            const int total = totalSlots();
            offset_ = std::clamp(drag_start_offset_ + shift, 0,
                                 std::max(0, total - visible_bars_));
            recomputeScale();
        }
    }

    if (clamped != hover_slot_) {
        hover_slot_ = clamped;
        emit hoveredIndexChanged(clamped);
    }
    update();
}

void CandleChartWidget::mousePressEvent(QMouseEvent* event) {
    if (event->button() != Qt::LeftButton) {
        QWidget::mousePressEvent(event);
        return;
    }
    drag_.press(static_cast<int>(event->position().x()));
    // 以按下瞬间的视图为平移基准。每次按下都重新取一次，避免多次拖动
    // 之间累加基准漂移（旧实现也是这么做的，保留）。
    drag_start_offset_ = offset_;
    if (drag_mode_) setCursor(Qt::ClosedHandCursor);   // "抓住"的反馈
    event->accept();
}

void CandleChartWidget::mouseReleaseEvent(QMouseEvent* event) {
    if (event->button() != Qt::LeftButton) {
        QWidget::mouseReleaseEvent(event);
        return;
    }

    // 按下点与释放点几乎重合 → 这是一次**点击**，切换平移模式。
    //
    // 这一句正是旧实现缺的：它把"按下"直接当成"开始拖动"，而唯一复位它的
    // 地方是 leaveEvent —— 于是单击一次之后图表就永久跟着鼠标跑，直到用户
    // 把指针移出控件。没有报错、没有崩溃，纯粹是手感坏掉。
    const int x = static_cast<int>(event->position().x());
    if (drag_.release(x)) {
        setDragMode(!drag_mode_);
    } else {
        syncCursor();   // 拖动结束：把"抓住"的光标还回去
    }
    event->accept();
}

void CandleChartWidget::leaveEvent(QEvent*) {
    hover_slot_ = -1;
    // **刻意不复位 drag_mode_**：平移模式是用户显式选择的模式，不该因为
    // 指针划过边界就悄悄关掉 —— 那会让人觉得"模式怎么又没了"。
    // 同理也不手动清 drag_：Qt 有鼠标抓取，按下后拖到控件外再松开，
    // 释放事件仍然会回到这里，配对不会丢。
    emit hoveredIndexChanged(-1);
    update();
}

}  // namespace fp::gui
