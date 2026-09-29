cmake_minimum_required(VERSION 3.16)
project(creational_patterns_lab LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_executable(creational_patterns main.cpp)
if(MSVC)
  target_compile_options(creational_patterns PRIVATE /W4 /utf-8)
else()
  target_compile_options(creational_patterns PRIVATE -Wall -Wextra -Wpedantic)
endif()
