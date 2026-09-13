cmake_minimum_required(VERSION 3.20)
project(HelloWorldC LANGUAGES C)
add_executable(hello main.c)
set_target_properties(hello PROPERTIES C_STANDARD 17 C_STANDARD_REQUIRED YES C_EXTENSIONS NO)
