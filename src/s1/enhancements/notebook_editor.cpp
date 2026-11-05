#include "s1/enhancements/notebook_editor.hpp"

#if SHENMUE_ENHANCEMENT_S1_QTE_SELECT && SHENMUE_IS_S1 && SHENMUE_IS_PC && defined(_WIN32)

#include "s1/flags.hpp"
#include "s1/player_stats.hpp"
#include "s1/task/mmtk.hpp"

#include "imgui.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <string>
#include <string_view>
#include <vector>

namespace shenmue::s1 {
extern std::array<std::uint8_t, 128> g_shenmueFlags;
}

namespace shenmue::s1::enhancements {
namespace {

// The notebook layout tables (kLoadingFlags, kStoryMemoFlags), as the notebook task uses them.
#include "s1/task/mmtk_notebook_flags.inc"

struct FlagName {
    std::uint16_t id;
    const char *name;
};

constexpr FlagName kFlagNames[] = {
#define SHENMUE_S1_FLAG(name, value) {value, #name},
#include "common/script_catalogue.def"
#undef SHENMUE_S1_FLAG
};

constexpr std::size_t kFlagCount = 1024;
// Record word 0 holds the page in its low byte; 0x80 there ends the table.
constexpr std::uint16_t kPageMask = 0xff;
constexpr std::uint16_t kTableEnd = 0x80;

struct NotebookLine {
    std::uint16_t page;
    std::uint16_t flag;
    std::string label;
};

constexpr float kWindowX = 560.0f;
constexpr float kWindowY = 12.0f;
constexpr float kWindowWidth = 520.0f;
constexpr float kWindowHeight = 560.0f;
constexpr std::size_t kFilterCapacity = 64;

bool FlagIsSet(std::uint16_t flag) {
    return flag < kFlagCount && (g_shenmueFlags[flag >> 3U] & static_cast<std::uint8_t>(1U << (flag & 7U))) != 0;
}

const char *FlagNameOf(std::uint16_t flag) {
    for (const FlagName &entry : kFlagNames) {
        if (entry.id == flag)
            return entry.name;
    }
    return nullptr;
}

// Unique flags per notebook page, from both language variants of the layout.
std::vector<NotebookLine> BuildLines() {
    std::vector<NotebookLine> lines;
    const auto collect = [&](const auto &table) {
        for (std::size_t record = 0; record + kNotebookFlagRecordWords <= table.size(); record += kNotebookFlagRecordWords) {
            const auto page = static_cast<std::uint16_t>(table[record] & kPageMask);
            if (page == kTableEnd)
                break;
            for (std::size_t line = 1; line < kNotebookFlagRecordWords; ++line) {
                const std::uint16_t flag = table[record + line];
                if (flag == 0 || flag >= kFlagCount)
                    continue;
                const bool known = std::any_of(lines.begin(), lines.end(), [&](const NotebookLine &l) { return l.page == page && l.flag == flag; });
                if (known)
                    continue;
                const char *const name = FlagNameOf(flag);
                std::array<char, 16> id{};
                std::snprintf(id.data(), id.size(), " (%u)", static_cast<unsigned>(flag));
                lines.push_back({page, flag, std::string(name ? name : "FLAG") + id.data()});
            }
        }
    };
    collect(kLoadingFlags);
    collect(kStoryMemoFlags);
    std::stable_sort(lines.begin(), lines.end(), [](const NotebookLine &a, const NotebookLine &b) { return a.page < b.page; });
    return lines;
}

const std::vector<NotebookLine> &Lines() {
    static const std::vector<NotebookLine> lines = BuildLines();
    return lines;
}

bool ContainsCaseInsensitive(std::string_view text, std::string_view needle) {
    if (needle.empty())
        return true;
    const auto lower = [](char c) { return static_cast<char>(std::tolower(static_cast<unsigned char>(c))); };
    return std::search(text.begin(), text.end(), needle.begin(), needle.end(), [&](char a, char b) { return lower(a) == lower(b); }) != text.end();
}

std::array<char, kFilterCapacity> g_filter{};

} // namespace

void SetStoryFlag(std::uint16_t flag, bool value) {
    if (flag >= kFlagCount)
        return;
    if (value)
        MMTK_UpdateEntryForFlag(flag);
    const auto mask = static_cast<std::uint8_t>(1U << (flag & 7U));
    std::uint8_t &byte = g_shenmueFlags[flag >> 3U];
    byte = value ? static_cast<std::uint8_t>(byte | mask) : static_cast<std::uint8_t>(byte & ~mask);
    RegisterInventoryItemGroup(flag);
}

void DrawNotebookEditor() {
    ImGui::SetNextWindowPos(ImVec2(kWindowX, kWindowY), ImGuiCond_FirstUseEver);
    ImGui::SetNextWindowSize(ImVec2(kWindowWidth, kWindowHeight), ImGuiCond_FirstUseEver);
    // Start collapsed so the game stays visible; the title bar opens it.
    ImGui::SetNextWindowCollapsed(true, ImGuiCond_FirstUseEver);
    if (!ImGui::Begin("Блокнот", nullptr, ImGuiWindowFlags_NoSavedSettings)) {
        ImGui::End();
        return;
    }
    ImGui::TextUnformatted("Записи появляются при следующем открытии блокнота.");
    ImGui::InputTextWithHint("##filter", "фильтр по имени", g_filter.data(), g_filter.size());
    const std::string_view filter(g_filter.data());

    ImGui::BeginChild("lines");
    std::int32_t openPage = -1;
    bool pageVisible = false;
    for (const NotebookLine &line : Lines()) {
        if (!ContainsCaseInsensitive(line.label, filter))
            continue;
        if (line.page != openPage) {
            if (pageVisible)
                ImGui::TreePop();
            openPage = line.page;
            std::array<char, 32> title{};
            std::snprintf(title.data(), title.size(), "Страница %u", static_cast<unsigned>(line.page));
            pageVisible = ImGui::TreeNodeEx(title.data(), filter.empty() ? 0 : ImGuiTreeNodeFlags_DefaultOpen);
        }
        if (!pageVisible)
            continue;
        bool enabled = FlagIsSet(line.flag);
        ImGui::PushID(static_cast<int>(line.page * kFlagCount + line.flag));
        if (ImGui::Checkbox(line.label.c_str(), &enabled))
            SetStoryFlag(line.flag, enabled);
        ImGui::PopID();
    }
    if (pageVisible)
        ImGui::TreePop();
    ImGui::EndChild();
    ImGui::End();
}

} // namespace shenmue::s1::enhancements

#endif
