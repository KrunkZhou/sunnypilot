#pragma once

#include <fstream>
#include <string>

inline std::string read_kernel_boot_id(const char *path = "/proc/sys/kernel/random/boot_id") {
  std::ifstream file(path);
  std::string value;
  if (!std::getline(file, value) || value.size() != 36) return {};
  bool nonzero = false;
  for (size_t i = 0; i < value.size(); ++i) {
    if (i == 8 || i == 13 || i == 18 || i == 23) {
      if (value[i] != '-') return {};
    } else {
      if (!((value[i] >= '0' && value[i] <= '9') || (value[i] >= 'a' && value[i] <= 'f'))) return {};
      nonzero |= value[i] != '0';
    }
  }
  return nonzero ? value : std::string{};
}

inline const std::string &get_kernel_boot_id() {
  static const std::string boot_id = read_kernel_boot_id();
  return boot_id;
}
