add_executable(finpulse-tests
    test_main.cpp
    test_json.cpp
    test_topic.cpp
    test_datashub.cpp
    test_codec.cpp
    test_types.cpp
    test_toolbridge.cpp
    test_agentservice.cpp
    test_eventtext.cpp
    test_chartinteraction.cpp
    test_profile.cpp
    test_chatrender.cpp
)

target_include_directories(finpulse-tests PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(finpulse-tests PRIVATE finpulse_core)

add_test(NAME unit COMMAND finpulse-tests)
# 集成用例会拉起 Python 子进程并跑完两轮投委会，比纯单测慢得多。
set_tests_properties(unit PROPERTIES TIMEOUT 600)
