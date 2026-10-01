// FinPulse Terminal — 行情表格模型
//
// 表格本身不订阅总线，而是由 MainWindow 在收到 quote 事件后调 upsertQuote。
// 这样模型保持"纯数据结构"的身份，可以被单测，也不关心数据从哪来。
#pragma once

#include <QAbstractTableModel>
#include <QColor>
#include <QHash>
#include <QVector>

#include "model/Types.h"

namespace fp::gui {

class QuoteTableModel : public QAbstractTableModel {
    Q_OBJECT

public:
    enum Column {
        ColSymbol = 0,
        ColLast,
        ColChange,
        ColChangePct,
        ColHigh,
        ColLow,
        ColVolume,
        ColTime,
        ColumnCount
    };

    explicit QuoteTableModel(QObject* parent = nullptr);

    int      rowCount(const QModelIndex& parent = QModelIndex()) const override;
    int      columnCount(const QModelIndex& parent = QModelIndex()) const override;
    QVariant data(const QModelIndex& index, int role = Qt::DisplayRole) const override;
    QVariant headerData(int section, Qt::Orientation orientation,
                        int role = Qt::DisplayRole) const override;

    /// 该列是否为数字列（决定右对齐方式）。
    static bool isNumericColumn(int section);

public slots:
    /// 已存在的符号就地更新，否则新增一行。
    void upsertQuote(const fp::Quote& quote);
    void clearAll();

private:
    QVector<fp::Quote>  quotes_;
    QHash<QString, int> row_of_symbol_;
};

}  // namespace fp::gui
