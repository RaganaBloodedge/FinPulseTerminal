// 版本号。C++ 侧与 python/finpulse_engine/__init__.py 的 __version__ 必须一致——
// handshake 会把两边的版本一起报出来，对不上时一眼就能看出是哪边没更新。
#pragma once

namespace fp {

inline constexpr const char* kVersion = "0.4.0";
inline constexpr const char* kAppName = "FinPulse Terminal";
inline constexpr const char* kEngineName = "finpulse-engine";

}  // namespace fp
