#include "TestFramework.h"

#include <cstdio>
#include <string>

int main(int argc, char** argv) {
    const std::string filter = argc > 1 ? std::string(argv[1]) : std::string();

    std::printf("FinPulse Terminal — 单元测试\n");
    std::printf("=============================\n");

    return fp::test::run_all(filter);
}
