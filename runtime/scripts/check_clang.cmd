@echo off
rem Stops the build early when the clang-cl on PATH is too old for the Visual Studio C++ library vcvars64 set up. Called by env.cmd
rem and recomp\scripts\nx_env.cmd after they set PATH. The MSVC 14.52 STL (Visual Studio 18) refuses clang older than 22 with
rem error STL1000 in yvals_core.h, deep into the build; this says so up front. Older MSVC with an older clang still builds, so that
rem only gets a warning, as does any clang below the LLVM 23 docs\BUILDING.md asks for.
set "NX_CLANG_MAJOR="
set "NX_VC_MINOR="
for /f "tokens=1-3" %%a in ('clang-cl --version 2^>nul') do if "%%a %%b"=="clang version" for /f "delims=." %%m in ("%%c") do set "NX_CLANG_MAJOR=%%m"
if defined VCToolsVersion for /f "tokens=2 delims=." %%m in ("%VCToolsVersion%") do set "NX_VC_MINOR=%%m"
if not defined NX_CLANG_MAJOR (
  echo [build] clang-cl was not found ^(looked in "%NX_LLVM_BIN%%NX_LLVM%" and on PATH^). Install LLVM 23 or newer from github.com/llvm/llvm-project/releases.
  exit /b 1
)
if not defined NX_VC_MINOR set "NX_VC_MINOR=0"
if %NX_CLANG_MAJOR% LSS 22 if %NX_VC_MINOR% GEQ 52 (
  echo [build] clang-cl %NX_CLANG_MAJOR% is too old for this Visual Studio's C++ library ^(MSVC %VCToolsVersion% needs clang 22 or newer^).
  echo [build] Install LLVM 23 or newer from github.com/llvm/llvm-project/releases, see docs\BUILDING.md.
  exit /b 1
)
if %NX_CLANG_MAJOR% LSS 23 echo [build] warning: clang-cl %NX_CLANG_MAJOR% is older than the LLVM 23 docs\BUILDING.md asks for.
exit /b 0
