#pragma once

// RABIT (Range-Aligned BItmap Table) - per-segment range-encoded bitmap index
//
// Like CUBIT, we partition each vector (up to STANDARD_VECTOR_SIZE rows) into
// B bins using the same quantile scheme.  The KEY DIFFERENCE is the bitmap
// encoding:
//
//   CUBIT : bin[b] stores rows whose value == bin_b  (equality-encoded)
//           Range query v < x  →  OR k bitmaps  (O(k * ceil(N/64)))
//
//   RABIT : bin[b] stores rows whose value <= endpoint[b] (range/cumulative)
//           Range query v < x  →  single bitmap copy  (O(ceil(N/64)))
//
// RABIT wins for HIGH-selectivity predicates (large k) because it replaces
// k bitmap OR operations with exactly one bitmap copy.  Build cost is
// O(B * N/64) extra over CUBIT (to compute prefix-OR of equality bitmaps).

#include <vector>
#include <algorithm>
#include <cstdint>
#include <cstddef>
#include <type_traits>
#include "duckdb/common/types/selection_vector.hpp"

namespace duckdb {

static constexpr size_t RABIT_MAX_BINS = 256;

// ---------------------------------------------------------------------------
// RabitBinIndex<T>  – the concrete, fully-templated range-bitmap index
// ---------------------------------------------------------------------------
template<typename T>
class RabitBinIndex {
    static_assert(std::is_same<T, uint32_t>::value || std::is_same<T, uint64_t>::value,
                  "RabitBinIndex: T must be uint32_t or uint64_t");
public:
    RabitBinIndex() = default;
    explicit RabitBinIndex(const std::vector<T> &data);

    // Range evaluation: populate msel and return matching row count.
    idx_t evaluate_less_than(T x, ManagedSelection &msel) const;
    idx_t evaluate_lessthan_orequal(T x, ManagedSelection &msel) const;
    idx_t evaluate_greater_than(T x, ManagedSelection &msel) const;
    idx_t evaluate_greaterthan_orequal(T x, ManagedSelection &msel) const;

    size_t GetNRows() const { return n_rows_; }
    size_t GetNBins() const { return n_bins_; }

private:
    void FillAllRows(ManagedSelection &msel) const;
    void ClearSelection(ManagedSelection &msel) const;
    void MaterializeSelection(ManagedSelection &msel) const;
    // Compute complement of src into dest (dest[w] = full_bitmap_[w] & ~src[w])
    void ComplementBitmap(const std::vector<uint64_t> &src, std::vector<uint64_t> &dest) const;
    // Fill dest with rows from the given bin whose values satisfy pred.
    template<typename Pred>
    void AddBoundaryBin(size_t bin, Pred pred, std::vector<uint64_t> &dest) const;

    size_t n_rows_  = 0;
    size_t n_bins_  = 0;
    size_t n_words_ = 0;
    T min_data_ {};
    T max_data_ {};

    std::vector<T>        endpoints_;    // quantile boundaries (same as CUBIT)
    std::vector<uint8_t>  codes_;        // bin code per row
    std::vector<T>        base_data_;    // original values for boundary-bin check

    // range_bitmaps_[b]: rows where value <= endpoint[b]  (cumulative OR)
    std::vector<std::vector<uint64_t>> range_bitmaps_;
    std::vector<uint64_t> full_bitmap_;  // all n_rows_ bits set
};

// ---------------------------------------------------------------------------
// Base class for polymorphic storage in ColumnData
// ---------------------------------------------------------------------------
struct BaseRabitIndex {
    virtual ~BaseRabitIndex() = default;
    virtual std::shared_ptr<BaseRabitIndex> Copy() const = 0;
};

template<typename T>
struct RabitBinIndexWrapper : public BaseRabitIndex {
    RabitBinIndex<T> impl;
    explicit RabitBinIndexWrapper(const std::vector<T> &data) : impl(data) {}
    RabitBinIndexWrapper(const RabitBinIndexWrapper &) = default;
    std::shared_ptr<BaseRabitIndex> Copy() const override {
        return std::make_shared<RabitBinIndexWrapper<T>>(*this);
    }
};

// ===========================================================================
// Template implementation
// ===========================================================================

template<typename T>
RabitBinIndex<T>::RabitBinIndex(const std::vector<T> &data)
    : base_data_(data), n_rows_(data.size()) {
    if (n_rows_ == 0) return;

    n_words_ = (n_rows_ + 63) / 64;

    std::vector<T> sorted = data;
    std::sort(sorted.begin(), sorted.end());
    min_data_ = sorted.front();
    max_data_ = sorted.back();

    {
        std::vector<T> uniq = sorted;
        size_t n_unique = static_cast<size_t>(std::unique(uniq.begin(), uniq.end()) - uniq.begin());
        n_bins_ = std::min(n_unique, RABIT_MAX_BINS);
    }

    // Quantile endpoints (same scheme as CUBIT / ColumnSketch)
    endpoints_.resize(n_bins_);
    size_t n = sorted.size();
    for (size_t i = 0; i < n_bins_; ++i) {
        size_t idx = (i + 1) * n / n_bins_ - 1;
        endpoints_[i] = sorted[std::min(idx, n - 1)];
    }

    // Assign bin codes
    codes_.resize(n_rows_);
    for (size_t i = 0; i < n_rows_; ++i) {
        auto it = std::lower_bound(endpoints_.begin(), endpoints_.end(), data[i]);
        size_t code = static_cast<size_t>(it - endpoints_.begin());
        codes_[i] = static_cast<uint8_t>(std::min(code, n_bins_ - 1));
    }

    // Build equality bitmaps, then prefix-OR into range bitmaps
    // range_bitmaps_[b] = cumulative OR of equality bitmaps 0..b
    range_bitmaps_.assign(n_bins_, std::vector<uint64_t>(n_words_, 0));

    // Step 1: populate equality bitmaps into range_bitmaps_ (reuse storage)
    for (size_t i = 0; i < n_rows_; ++i) {
        range_bitmaps_[codes_[i]][i / 64] |= (1ULL << (i % 64));
    }

    // Step 2: compute prefix OR in-place
    for (size_t b = 1; b < n_bins_; ++b) {
        for (size_t w = 0; w < n_words_; ++w) {
            range_bitmaps_[b][w] |= range_bitmaps_[b - 1][w];
        }
    }

    // Precompute full bitmap
    full_bitmap_.assign(n_words_, ~static_cast<uint64_t>(0));
    size_t rem = n_rows_ % 64;
    if (rem != 0) {
        full_bitmap_[n_words_ - 1] = (1ULL << rem) - 1;
    }
}

template<typename T>
void RabitBinIndex<T>::FillAllRows(ManagedSelection &msel) const {
    msel.bitmask = full_bitmap_;
    SelectionVector &sel = msel.Selection();
    sel.Initialize(n_rows_);
    for (idx_t i = 0; i < static_cast<idx_t>(n_rows_); ++i) {
        sel.set_index(i, i);
    }
    msel.SetCount(static_cast<idx_t>(n_rows_));
}

template<typename T>
void RabitBinIndex<T>::ClearSelection(ManagedSelection &msel) const {
    msel.bitmask.assign(n_words_, 0);
    msel.SetCount(0);
}

template<typename T>
void RabitBinIndex<T>::MaterializeSelection(ManagedSelection &msel) const {
    SelectionVector &sel = msel.Selection();
    sel.Initialize(n_rows_);
    idx_t cnt = 0;
    for (size_t w = 0; w < n_words_; ++w) {
        uint64_t mask = msel.bitmask[w];
        while (mask) {
            int bit = __builtin_ctzll(mask);
            sel.set_index(cnt++, static_cast<sel_t>(w * 64 + static_cast<size_t>(bit)));
            mask &= mask - 1;
        }
    }
    msel.SetCount(cnt);
}

template<typename T>
void RabitBinIndex<T>::ComplementBitmap(const std::vector<uint64_t> &src,
                                         std::vector<uint64_t> &dest) const {
    dest.resize(n_words_);
    for (size_t w = 0; w < n_words_; ++w) {
        dest[w] = full_bitmap_[w] & ~src[w];
    }
}

template<typename T>
template<typename Pred>
void RabitBinIndex<T>::AddBoundaryBin(size_t bin, Pred pred,
                                       std::vector<uint64_t> &dest) const {
    for (size_t i = 0; i < n_rows_; ++i) {
        if (codes_[i] == static_cast<uint8_t>(bin) && pred(base_data_[i])) {
            dest[i / 64] |= (1ULL << (i % 64));
        }
    }
}

// ---------------------------------------------------------------------------
// evaluate_less_than: v < x
// ---------------------------------------------------------------------------
template<typename T>
idx_t RabitBinIndex<T>::evaluate_less_than(T x, ManagedSelection &msel) const {
    if (n_rows_ == 0) { msel.SetCount(0); return 0; }

    // All values <= max_data_; if x > max_data_ all rows qualify
    if (x > max_data_) { FillAllRows(msel); return msel.Count(); }
    // If x <= min_data_ no row qualifies (strict less-than)
    if (x <= min_data_) { ClearSelection(msel); return 0; }

    // Find last bin b where endpoints_[b] < x  (strictly less than)
    // These bins are fully included.
    // bins where endpoint[b] < x → lower_bound(endpoints_, x) gives first b where endpoint[b] >= x
    // So fully included bins: 0 .. (it - 1)
    auto it = std::lower_bound(endpoints_.begin(), endpoints_.end(), x);
    // it points to first endpoint >= x
    size_t boundary_bin = static_cast<size_t>(it - endpoints_.begin());
    // bins 0..boundary_bin-1 are fully included; boundary_bin is the partial bin

    msel.bitmask.assign(n_words_, 0);

    if (boundary_bin > 0) {
        // Copy cumulative bitmap for bins 0..boundary_bin-1
        msel.bitmask = range_bitmaps_[boundary_bin - 1];
    }

    // Add partial rows from boundary_bin (values strictly < x)
    if (boundary_bin < n_bins_) {
        AddBoundaryBin(boundary_bin, [x](T v) { return v < x; }, msel.bitmask);
    }

    MaterializeSelection(msel);
    return msel.Count();
}

// ---------------------------------------------------------------------------
// evaluate_lessthan_orequal: v <= x
// ---------------------------------------------------------------------------
template<typename T>
idx_t RabitBinIndex<T>::evaluate_lessthan_orequal(T x, ManagedSelection &msel) const {
    if (n_rows_ == 0) { msel.SetCount(0); return 0; }

    if (x >= max_data_) { FillAllRows(msel); return msel.Count(); }
    if (x < min_data_)  { ClearSelection(msel); return 0; }

    // Find last bin b where endpoints_[b] <= x
    // upper_bound gives first endpoint > x → subtract 1
    auto it = std::upper_bound(endpoints_.begin(), endpoints_.end(), x);
    size_t last_full_bin = static_cast<size_t>(it - endpoints_.begin());
    // bins 0..last_full_bin-1 have endpoint <= x → fully included
    // last_full_bin might be the boundary bin (endpoint > x but some rows <= x)

    msel.bitmask.assign(n_words_, 0);

    if (last_full_bin > 0) {
        msel.bitmask = range_bitmaps_[last_full_bin - 1];
    }

    // Add partial rows from last_full_bin bin (values <= x)
    if (last_full_bin < n_bins_) {
        AddBoundaryBin(last_full_bin, [x](T v) { return v <= x; }, msel.bitmask);
    }

    MaterializeSelection(msel);
    return msel.Count();
}

// ---------------------------------------------------------------------------
// evaluate_greater_than: v > x  (complement of v <= x)
// ---------------------------------------------------------------------------
template<typename T>
idx_t RabitBinIndex<T>::evaluate_greater_than(T x, ManagedSelection &msel) const {
    if (n_rows_ == 0) { msel.SetCount(0); return 0; }

    // Evaluate v <= x first, then complement
    ManagedSelection tmp(static_cast<idx_t>(n_rows_));
    evaluate_lessthan_orequal(x, tmp);

    ComplementBitmap(tmp.bitmask, msel.bitmask);
    MaterializeSelection(msel);
    return msel.Count();
}

// ---------------------------------------------------------------------------
// evaluate_greaterthan_orequal: v >= x  (complement of v < x)
// ---------------------------------------------------------------------------
template<typename T>
idx_t RabitBinIndex<T>::evaluate_greaterthan_orequal(T x, ManagedSelection &msel) const {
    if (n_rows_ == 0) { msel.SetCount(0); return 0; }

    ManagedSelection tmp(static_cast<idx_t>(n_rows_));
    evaluate_less_than(x, tmp);

    ComplementBitmap(tmp.bitmask, msel.bitmask);
    MaterializeSelection(msel);
    return msel.Count();
}

} // namespace duckdb
