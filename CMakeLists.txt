cmake_minimum_required(VERSION 3.20)

project(FinPulseTerminal
    VERSION 0.4.0
    DESCRIPTION "轻量级跨平台金融终端：C++20 桌面壳 + 嵌入式 Python 分析引擎"
    LANGUAGES CXX)

# ── 语言与默认构建类型 ────────────────────────────────────

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "构建类型" FORCE)
endif()

set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# ── 编译选项 ──────────────────────────────────────────────
# /utf-8（MSVC）和默认的 UTF-8（GCC/Clang）都是必需的：
# 源码里有中文注释和字符串字面量，编码没统一会直接报语法错。

if(MSVC)
    add_compile_options(/utf-8 /W4 /permissive-)
else()
    add_compile_options(-Wall -Wextra -Wpedantic)
endif()

# ── 选项 ──────────────────────────────────────────────────

option(FINPULSE_BUILD_GUI    "构建 Qt6 图形界面（找不到 Qt6 会自动跳过）" ON)
option(FINPULSE_BUILD_TESTS  "构建单元测试" ON)
option(FINPULSE_BUILD_TOOLS  "构建 tools/ 下的辅助程序" ON)

find_package(Threads REQUIRED)

# ── 核心库 ────────────────────────────────────────────────
# 刻意设计为零第三方依赖：只用 C++20 标准库 + 平台 API。
# 这样它可以被单独编译、单独测试，GUI 和 CLI 都只是它的使用者。

add_library(finpulse_core STATIC
    src/core/Json.cpp
    src/core/Profile.cpp
    src/core/Topic.cpp
    src/core/DataHub.cpp
    src/model/Types.cpp
    src/data/Watchlist.cpp
    src/data/ReplaySource.cpp
    src/bridge/FrameCodec.cpp
    src/bridge/Subprocess.cpp
    src/bridge/RpcClient.cpp
    src/bridge/PyEngine.cpp
    src/agent/AgentService.cpp
    src/agent/ToolBridge.cpp
)

target_include_directories(finpulse_core PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}/src
)

target_compile_definitions(finpulse_core PUBLIC
    FINPULSE_PYTHON_ROOT="${CMAKE_CURRENT_SOURCE_DIR}/python"
    FINPULSE_VERSION="${PROJECT_VERSION}"
)

target_link_libraries(finpulse_core PUBLIC Threads::Threads)

# ── 命令行前端 ────────────────────────────────────────────

add_executable(finpulse-cli
    src/app/main.cpp
    src/app/CliApp.cpp
)
target_link_libraries(finpulse-cli PRIVATE finpulse_core)

# ── 辅助工具 ──────────────────────────────────────────────

if(FINPULSE_BUILD_TOOLS)
    add_executable(finpulse-bench tools/bench_bridge.cpp)
    target_link_libraries(finpulse-bench PRIVATE finpulse_core)
endif()

# ── 测试 ──────────────────────────────────────────────────

if(FINPULSE_BUILD_TESTS)
    enable_testing()
    add_subdirectory(tests)
endif()

# ── 图形界面 ──────────────────────────────────────────────

if(FINPULSE_BUILD_GUI)
    find_package(Qt6 6.4 COMPONENTS Widgets QUIET)
    if(Qt6_FOUND)
        message(STATUS "找到 Qt6 ${Qt6_VERSION}，将构建图形界面")
        add_subdirectory(src/gui)
    else()
        message(STATUS "未找到 Qt6 —— 跳过图形界面。"
                       "命令行版不受影响，仍可完整使用。"
                       "如需 GUI 请设置 CMAKE_PREFIX_PATH 指向 Qt6 安装位置。")
    endif()
endif()

message(STATUS "")
message(STATUS "FinPulse Terminal ${PROJECT_VERSION}")
message(STATUS "  构建类型 : ${CMAKE_BUILD_TYPE}")
message(STATUS "  编译器   : ${CMAKE_CXX_COMPILER_ID} ${CMAKE_CXX_COMPILER_VERSION}")
message(STATUS "  引擎路径 : ${CMAKE_CURRENT_SOURCE_DIR}/python")
message(STATUS "")
