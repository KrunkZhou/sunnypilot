#include <cstdio>
#include <fstream>
#include <string>
#include <unistd.h>

#include "common/diagnostic_timing.h"
#include "common/tests/native_test.h"

void test_boot_identity() {
  char path[] = "/tmp/diagnostic-boot-XXXXXX";
  const int fd = mkstemp(path);
  CHECK(fd >= 0);
  close(fd);
  const std::string boot_id = "c08e8b38-8f57-4f39-a8c0-8bf894992b54";
  for (const auto &value : {boot_id, std::string("invalid"), std::string("00000000-0000-0000-0000-000000000000"),
                            std::string("c08e8b388f574f39a8c08bf894992b54")}) {
    { std::ofstream file(path); file << value << '\n'; }
    CHECK(read_kernel_boot_id(path) == (value == boot_id ? boot_id : std::string{}));
  }
  CHECK(std::remove(path) == 0);
  CHECK(read_kernel_boot_id(path).empty());
}

int main() {
  return run_native_test(test_boot_identity);
}
