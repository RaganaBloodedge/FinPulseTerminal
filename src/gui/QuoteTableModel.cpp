#include "QuoteTableModel.h"

#include <QBrush>
#include <QFont>
#include <QLocale>

namespace fp::gui {

namespace {

// 中国市场习惯：涨红跌绿。这与欧美相反，但与用户每天看到的看盘软件一致。
const QColor kUpColor(0xD1, 0x2B, 0x2B);
const QColor kDownColor(0x1E, 0x8E, 0x3E);
const QColor kFlatColor(0x60, 0x60, 0x60);

QColor trendColor(double change) {
    if (change > 0.0) return kUpColor;
    if (change < 0.0) return kDownColor;
    return kFlatColor;
}

QString humanVolume(long long v) {
    const double d = static_cast<double>(v);
    if (d >= 1e8) return QString::number(d / 1e8, 'f', 2) + QStringLiteral(" 亿");
    if (d >= 1e4) return QString::number(d / 1e4, 'f', 2) + QStringLiteral(" 万");
    return QLocale::system().toString(v);
}

}  // namespace

QuoteTableModel::QuoteTableModel(QObject* parent) : QAbstractTableModel(parent) {}

int QuoteTableModel::rowCount(const QModelIndex& parent) const {
    return parent.isValid() ? 0 : quotes_.size();
}

int QuoteTableModel::columnCount(const QModelIndex& parent) const {
    return parent.isValid() ? 0 : ColumnCount;
}

bool QuoteTableModel::isNumericColumn(int section) {
    return section != ColSymbol && section != ColTime;
}

QVariant QuoteTableModel::data(const QModelIndex& index, int role) const {
    if (!index.isValid() || index.row() >= quotes_.size()) return {};
    const Quote& q = quotes_.at(index.row());

    switch (role) {
        case Qt::DisplayRole:
            switch (index.column()) {
                case ColSymbol:    return QString::fromStdString(q.symbol);
                case ColLast:      return QString::number(q.last, 'f', 2);
                case ColChange:    return QString::number(q.change(), 'f', 2);
                case ColChangePct: return QString::number(q.change_pct(), 'f', 2) + "%";
                case ColHigh:      return QString::number(q.high, 'f', 2);
                case ColLow:       return QString::number(q.low, 'f', 2);
                case ColVolume:    return humanVolume(q.volume);
                case ColTime:      return QString::fromStdString(format_date(q.ts_ms));
                default:           return {};
            }

        case Qt::TextAlignmentRole:
            return isNumericColumn(index.column())
                       ? QVariant(static_cast<int>(Qt::AlignRight | Qt::AlignVCenter))
                       : QVariant(static_cast<int>(Qt::AlignLeft | Qt::AlignVCenter));

        case Qt::ForegroundRole:
            // 涨跌列用红绿区分，和看盘软件一致
            if (index.column() == ColChange || index.column() == ColChangePct ||
                index.column() == ColLast) {
                return QBrush(trendColor(q.change()));
            }
            return {};

        case Qt::FontRole: {
            QFont f;
            if (index.column() == ColSymbol) {
                f.setBold(true);
            } else {
                f.setFamilies({QStringLiteral("Consolas"), QStringLiteral("Menlo"),
                               QStringLiteral("DejaVu Sans Mono")});
            }
            return f;
        }

        case Qt::ToolTipRole:
            return QStringLiteral("%1\n收盘 %2\n成交量 %3\n时间 %4")
                .arg(QString::fromStdString(q.symbol))
                .arg(q.last, 0, 'f', 4)
                .arg(q.volume)
                .arg(QString::fromStdString(format_datetime(q.ts_ms)));

        default:
            return {};
    }
}

QVariant QuoteTableModel::headerData(int section, Qt::Orientation orientation, int role) const {
    if (role != Qt::DisplayRole) return {};
    if (orientation == Qt::Vertical) return section + 1;

    switch (section) {
        case ColSymbol:    return QStringLiteral("标的");
        case ColLast:      return QStringLiteral("最新价");
        case ColChange:    return QStringLiteral("涨跌");
        case ColChangePct: return QStringLiteral("涨跌幅");
        case ColHigh:      return QStringLiteral("最高");
        case ColLow:       return QStringLiteral("最低");
        case ColVolume:    return QStringLiteral("成交量");
        case ColTime:      return QStringLiteral("日期");
        default:           return {};
    }
}

void QuoteTableModel::upsertQuote(const Quote& quote) {
    if (quote.symbol.empty()) return;

    const QString key = QString::fromStdString(quote.symbol);
    const auto    it  = row_of_symbol_.constFind(key);

    if (it != row_of_symbol_.constEnd()) {
        const int row = it.value();
        quotes_[row] = quote;
        // 整行刷新：一列变化基本意味着整行都变了，逐列发信号没必要
        emit dataChanged(index(row, 0), index(row, ColumnCount - 1));
        return;
    }

    const int row = quotes_.size();
    beginInsertRows(QModelIndex(), row, row);
    quotes_.append(quote);
    row_of_symbol_.insert(key, row);
    endInsertRows();
}

void QuoteTableModel::clearAll() {
    if (quotes_.isEmpty()) return;
    beginResetModel();
    quotes_.clear();
    row_of_symbol_.clear();
    endResetModel();
}

}  // namespace fp::gui
