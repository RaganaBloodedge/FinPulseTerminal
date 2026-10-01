# Qt6 图形界面。
#
# AUTOMOC 必须开：MainWindow / QuoteTableModel / CandleChartWidget 都有 Q_OBJECT，
# 不开的话编译能过但链接时会缺 vtable，报错信息还相当难懂。
set(CMAKE_AUTOMOC ON)
set(CMAKE_AUTORCC ON)

add_executable(finpulse-gui
    main_gui.cpp
    MainWindow.cpp
    MainWindow.h
    QuoteTableModel.cpp
    QuoteTableModel.h
    CandleChartWidget.cpp
    CandleChartWidget.h
    SettingsDialog.cpp
    SettingsDialog.h
)
# ChatRender.h 不必列进来：它是纯头文件（inline 函数），没有对应的 .cpp，
# AUTOMOC 也没有要扫的东西 —— 列进来只会让人以为它是个编译单元。

target_link_libraries(finpulse-gui PRIVATE finpulse_core Qt6::Widgets)

# GUI 依赖 finpulse_core 里那条编译期注入的引擎路径（FINPULSE_PYTHON_ROOT），
# 它是 PUBLIC 定义，所以这里不用重复设置。
