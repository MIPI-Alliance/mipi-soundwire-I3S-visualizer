// A cascade of second-order IIR sections over a double-precision signal: the recursion
// that numpy cannot vectorise, so the high-pass filter needs it here.
//
// Each row of `sos` is [b0, b1, b2, a0, a1, a2] with a0 == 1, and each section runs in
// transposed direct form II, the same arrangement and update order as scipy.signal.sosfilt,
// so the Python side's forward-backward wrapper (swi3s_studio/dsp/filters.py) reproduces
// sosfiltfilt to rounding. `zi` holds two state values per section and is updated in place
// to the final state.
#pragma once

#include <cstddef>

namespace swi3score {

inline void SosFilter(const double* sos, std::size_t nSections, const double* x, double* y,
                      std::size_t n, double* zi)
{
    for (std::size_t i = 0; i < n; ++i)
        y[i] = x[i];
    for (std::size_t s = 0; s < nSections; ++s) {
        const double* c = sos + 6 * s;
        const double b0 = c[0], b1 = c[1], b2 = c[2], a1 = c[4], a2 = c[5];
        double z0 = zi[2 * s], z1 = zi[2 * s + 1];
        for (std::size_t i = 0; i < n; ++i) {
            const double in = y[i];
            const double out = b0 * in + z0;
            z0 = b1 * in - a1 * out + z1;
            z1 = b2 * in - a2 * out;
            y[i] = out;
        }
        zi[2 * s] = z0;
        zi[2 * s + 1] = z1;
    }
}

}  // namespace swi3score
