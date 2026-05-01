#pragma once

// CUBIT (Compressed Updating BItmap index Table) - per-segment bitmap index
//
// For each row-group vector (up to STANDARD_VECTOR_SIZE rows), we maintain
// a set of B bins (B <= 256).  Each bin owns a complete uint64_t[] bitmap over
// all N rows in the vector.  A range predicate is evaluated by OR-ing together
// the bitmaps of the qualifying bins – O(k * ceil(N/64)) where k is the number
// of qualifying bins.  The sketch evaluates in O(N/64) regardless of k; CUBIT
// therefore wins for low-selectivity predicates (small k) and loses for
// high-selectivity ones.

#include <vector>
#include <algorithm>
#include <cstdint>
#include <cstddef>
#include <type_traits>
#include "duckdb/common/types/selection_vector.hpp"

namespace duckdb {

static constexpr size_t CUBIT_MAX_BINS = 256;

// ---------------------------------------------------------------------------
// CubitBinIndex<T>  – the concrete, fully-templated bitmap index
// ---------------------------------------------------------------------------
template<typename T>
class CubitBinIndex {
    static_assert(std::is_same<T, uint32_t>::value || std::is_same<T, uint64_t>::value,
                  "CubitBinIndex: T must be uint32_t or uint64_t");
public:
    CubitBinIndex() = default;
    explicit CubitBinIndex(const std::vector<T> &data);

    // Range evaluation methods – evaluate the predicate, write matching rows
    // into msel (both the bitmask and the SelectionVector), and return the count.
    idx_t evaluate_less_than(T x, ManagedSelection &msel) const;
    idx_t evaluate_lessthan_orequal(T x, ManagedSelection &msel) const;
    idx_t evaluate_greater_than(T x, ManagedSelection &msel) const;
    idx_t evaluate_greaterthan_orequal(T x, ManagedSelection &msel) const;

    size_t GetNRows()  const { return n_rows_; }
    size_t GetNBins()  const { return n_bins_; }

private:
    // Helper: fill msel with all n_rows_ entries.
    void FillAllRows(ManagedSelection &msel) const;
    // Helper: build SelectionVector from msel.bitmask (already populated).
    void MaterializeSelection(ManagedSelection &msel) const;
    // Helper: OR bin bitmaps in the half-open range [lo, hi) into dest.
    void OrBitmaps(size_t lo, size_t hi, std::vector<uint64_t> &dest) const;
    // Helper: add rows from the boundary bin whose base value satisfies pred.
    template<typename Pred>
    void AddBoundaryBin(size_t bin, Pred pred, std::vector<uint64_t> &dest) const;

    size_t n_rows_  = 0;
    size_t n_bins_  = 0;
    size_t n_words_ = 0;   // = ceil(n_rows_ / 64)
    T min_data_ {};
    T max_data_ {};
    bool need_check_base_ = true;

    std::vector<T>        endpoints_;     // quantile boundaries (same scheme as ColumnSketch)
    std::vector<uint8_t>  codes_;         // bin code for each row
    std::vector<T>        base_data_;     // original values (kept for boundary-bin verification)

    // bin_bitmaps_[b][w]: 64 bits of row membership for bin b, word w
    std::vector<std::vector<uint64_t>> bin_bitmaps_;
    // Precomputed bitmap with all n_rows_ low bits set.
    std::vector<uint64_t> full_bitmap_;
};

// ---------------------------------------------------------------------------
// Base class for polymorphic storage in ColumnData
// ---------------------------------------------------------------------------
struct BaseCubitIndex {
    virtual ~BaseCubitIndex() = default;
    virtual std::shared_ptr<BaseCubitIndex> Copy() const = 0;
};

template<typename T>
struct CubitBinIndexWrapper : public BaseCubitIndex {
    CubitBinIndex<T> impl;
    explicit CubitBinIndexWrapper(const std::vector<T> &data) : impl(data) {}
    CubitBinIndexWrapper(const CubitBinIndexWrapper &) = default;
    std::shared_ptr<BaseCubitIndex> Copy() const override {
        return std::make_shared<CubitBinIndexWrapper<T>>(*this);
    }
};

// ===========================================================================
// Template implementation
// ===========================================================================

template<typename T>
CubitBinIndex<T>::CubitBinIndex(const std::vector<T> &data)
    : base_data_(data), n_rows_(data.size()) {
    if (n_rows_ == 0) return;

    n_words_ = (n_rows_ + 63) / 64;

    // Sort data to compute quantile boundaries.
    std::vector<T> sorted = data;
    std::sort(sorted.begin(), sorted.end());
    min_data_ = sorted.front();
    max_data_ = sorted.back();

    // Count unique values to decide need_check_base_.
    {
        std::vector<T> uniq = sorted;
        size_t n_unique = static_cast<size_t>(std::unique(uniq.begin(), uniq.end()) - uniq.begin());
        need_check_base_ = (n_unique > CUBIT_MAX_BINS);
        n_bins_ = std::min(n_unique, CUBIT_MAX_BINS);
    }

    // Build endpoints – same quantile scheme as ColumnSketch::build_order_preserving_map.
    endpoints_.resize(n_bins_);
    size_t n = sorted.size();
    for (size_t i = 0; i < n_bins_; ++i) {
        size_t idx = (i + 1) * n / n_bins_ - 1;
        endpoints_[i] = sorted[std::min(idx, n - 1)];
    }

    // Assign a bin code to every row.
    codes_.resize(n_rows_);
    for (size_t i = 0; i < n_rows_; ++i) {
        auto it = std::lower_bound(endpoints_.begin(), endpoints_.end(), data[i]);
        size_t code = static_cast<size_t>(it - endpoints_.begin());
        codes_[i] = static_cast<uint8_t>(std::min(code, n_bins_ - 1));
    }

    // Allocate per-bin bitmaps and set membership bits.
    bin_bitmaps_.assign(n_bins_, std::vector<uint64_t>(n_words_, 0));
    for (size_t i = 0; i < n_rows_; ++i) {
        bin_bitmaps_[codes_[i]][i / 64] |= (1ULL << (i % 64));
    }

    // Precompute full bitmap (all n_rows_ rows qualify).
    full_bitmap_.assign(n_words_, ~static_cast<uint64_t>(0));
    size_t rem = n_rows_ % 64;
    if (rem != 0) {
        full_bitmap_[n_words_ - 1] = (1ULL << rem) - 1;
    }
}

template<typename T>
void CubitBinIndex<T>::FillAllRows(ManagedSelection &msel) const {
    msel.bitmask = full_bitmap_;
    SelectionVector &sel = msel.Selection();
    sel.Initialize(n_rows_);
    for (idx_t i = 0; i < static_cast<idx_t>(n_rows_); ++i) {
        sel.set_index(i, i);
    }
    msel.SetCount(static_cast<idx_t>(n_rows_));
}

template<typename T>
void CubitBinIndex<T>::MaterializeSelection(ManagedSelection &msel) const {
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
void CubitBinIndex<T>::OrBitmaps(size_t lo, size_t hi,
                                  std::vector<uint64_t> &dest) const {
    for (size_t b = lo; b < hi; ++b) {
        const auto &bm = bin_bitmaps_[b];
        for (size_t w = 0; w < n_words_; ++w) {
            dest[w] |= bm[w];
        }
    }
}

template<typename T>
template<typename Pred>
void CubitBinIndex<T>::AddBoundaryBin(size_t bin, Pred pred,
                                       std::vector<uint64_t> &dest) const {
    const auto &bm = bin_bitmaps_[bin];
    for (size_t w = 0; w < n_words_; ++w) {
        uint64_t wbits = bm[w];
        while (wbits) {
            int bit = __builtin_ctzll(wbits);
            size_t row = w * 64 + static_cast<size_t>(bit);
            if (row < n_rows_ && pred(base_data_[row])) {
                dest[w] |= (1ULL << bit);
            }
            wbits &= wbits - 1;
        }
    }
}

// ---------------------------------------------------------------------------
// evaluate_less_than: returns rows where value < x
// ---------------------------------------------------------------------------
template<typename T>
idx_t CubitBinIndex<T>::evaluate_less_than(T x, ManagedSelection &msel) const {
    if (n_rows_ == 0) { msel.SetCount(0); return 0; }

    msel.bitmask.assign(n_words_, 0);
    msel.Selection().Initialize(n_rows_);

    if (x <= min_data_) { msel.SetCount(0); return 0; }
    if (x > max_data_)  { FillAllRows(msel); return msel.Count(); }

    // Bins whose endpoint < x have all their values < x.
    size_t code_x = static_cast<size_t>(
        std::lower_bound(endpoints_.begin(), endpoints_.end(), x) - endpoints_.begin());

    OrBitmaps(0, code_x, msel.bitmask);

    // Boundary bin: rows with base_data < x (only when bin boundaries may not
    // be exact, i.e. need_check_base_ is true).
    if (need_check_base_ && code_x < n_bins_) {
        AddBoundaryBin(code_x, [x](T v){ return v < x; }, msel.bitmask);
    }

    MaterializeSelection(msel);
    return msel.Count();
}

// ---------------------------------------------------------------------------
// evaluate_lessthan_orequal: rows where value <= x
// ---------------------------------------------------------------------------
template<typename T>
idx_t CubitBinIndex<T>::evaluate_lessthan_orequal(T x, ManagedSelection &msel) const {
    if (n_rows_ == 0) { msel.SetCount(0); return 0; }

    msel.bitmask.assign(n_words_, 0);
    msel.Selection().Initialize(n_rows_);

    if (x < min_data_) { msel.SetCount(0); return 0; }
    if (x >= max_data_) {
        if (!need_check_base_ || x == max_data_) {
            FillAllRows(msel);
            return msel.Count();
        }
    }

    // code_x = first bin with endpoint >= x; all bins with endpoint <= x qualify.
    size_t code_x = static_cast<size_t>(
        std::upper_bound(endpoints_.begin(), endpoints_.end(), x) - endpoints_.begin());

    OrBitmaps(0, code_x, msel.bitmask);

    // Boundary bin: rows whose base value <= x.
    if (need_check_base_ && code_x < n_bins_) {
        AddBoundaryBin(code_x, [x](T v){ return v <= x; }, msel.bitmask);
    }

    MaterializeSelection(msel);
    return msel.Count();
}

// ---------------------------------------------------------------------------
// evaluate_greater_than: rows where value > x
// ---------------------------------------------------------------------------
template<typename T>
idx_t CubitBinIndex<T>::evaluate_greater_than(T x, ManagedSelection &msel) const {
    if (n_rows_ == 0) { msel.SetCount(0); return 0; }

    msel.bitmask.assign(n_words_, 0);
    msel.Selection().Initialize(n_rows_);

    if (x >= max_data_)  { msel.SetCount(0); return 0; }
    if (x < min_data_)   { FillAllRows(msel); return msel.Count(); }

    // All bins strictly after code_x qualify completely.
    size_t code_x = static_cast<size_t>(
        std::upper_bound(endpoints_.begin(), endpoints_.end(), x) - endpoints_.begin());

    OrBitmaps(code_x, n_bins_, msel.bitmask);

    // Boundary bin (index code_x - 1): rows with base_data > x.
    if (need_check_base_ && code_x > 0) {
        AddBoundaryBin(code_x - 1, [x](T v){ return v > x; }, msel.bitmask);
    }

    MaterializeSelection(msel);
    return msel.Count();
}

// ---------------------------------------------------------------------------
// evaluate_greaterthan_orequal: rows where value >= x
// ---------------------------------------------------------------------------
template<typename T>
idx_t CubitBinIndex<T>::evaluate_greaterthan_orequal(T x, ManagedSelection &msel) const {
    if (n_rows_ == 0) { msel.SetCount(0); return 0; }

    msel.bitmask.assign(n_words_, 0);
    msel.Selection().Initialize(n_rows_);

    if (x > max_data_) { msel.SetCount(0); return 0; }
    if (x <= min_data_) {
        if (!need_check_base_ || x == min_data_) {
            FillAllRows(msel);
            return msel.Count();
        }
    }

    // code_x = first bin with endpoint >= x; bins from code_x onward qualify.
    size_t code_x = static_cast<size_t>(
        std::lower_bound(endpoints_.begin(), endpoints_.end(), x) - endpoints_.begin());

    OrBitmaps(code_x, n_bins_, msel.bitmask);

    // Boundary bin (code_x - 1): rows with base_data >= x.
    if (need_check_base_ && code_x > 0) {
        AddBoundaryBin(code_x - 1, [x](T v){ return v >= x; }, msel.bitmask);
    }

    MaterializeSelection(msel);
    return msel.Count();
}

} // namespace duckdb
