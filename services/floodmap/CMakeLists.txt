cmake_minimum_required(VERSION 3.16)
project(floodmap_service CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

# Crow is header-only: point CROW_INCLUDE_DIR at its include/ folder (the one holding crow.h).
set(CROW_INCLUDE_DIR "/home/arash/Projects/crow/include" CACHE PATH "Crow include directory (contains crow.h)")
if(NOT EXISTS "${CROW_INCLUDE_DIR}/crow.h")
  message(FATAL_ERROR "crow.h not found in CROW_INCLUDE_DIR=${CROW_INCLUDE_DIR}")
endif()

# Standalone asio (Debian/Ubuntu: libasio-dev); header-only.
find_path(ASIO_INCLUDE_DIR asio.hpp PATHS /usr/include /usr/local/include)
if(NOT ASIO_INCLUDE_DIR)
  message(FATAL_ERROR "asio.hpp not found (install libasio-dev or set ASIO_INCLUDE_DIR)")
endif()

find_package(Threads REQUIRED)

add_executable(floodmap_service src/main.cpp)
target_include_directories(floodmap_service SYSTEM PRIVATE ${CROW_INCLUDE_DIR} ${ASIO_INCLUDE_DIR})
target_compile_definitions(floodmap_service PRIVATE ASIO_STANDALONE)
target_compile_options(floodmap_service PRIVATE -Wall -Wextra)
target_link_libraries(floodmap_service PRIVATE Threads::Threads)

install(TARGETS floodmap_service RUNTIME DESTINATION bin)
