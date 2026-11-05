#pragma once

#include "s1/enhancements/qte_select.hpp"

// Scene preset autotest for the QTE debug panel. With SHEN_QTE_AUTOTEST set, the game launches
// every scene preset in turn, watches whether its event starts (sequence camera, scripted scene,
// player control taken, QTE prompt), dumps frames, writes qte_autotest.txt next to the executable
// and quits. SHEN_QTE_AUTOTEST=all runs every preset; a comma list of indices runs those.
#if SHENMUE_ENHANCEMENT_S1_QTE_SELECT && SHENMUE_IS_S1 && SHENMUE_IS_PC && defined(_WIN32)

namespace shenmue::s1::enhancements {

// Game thread, once per frame.
void Autotest_Update();

} // namespace shenmue::s1::enhancements

#endif
