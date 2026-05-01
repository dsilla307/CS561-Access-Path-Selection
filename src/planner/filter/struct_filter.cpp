#include "duckdb/planner/filter/struct_filter.hpp"
#include "duckdb/storage/statistics/base_statistics.hpp"
#include "duckdb/storage/statistics/struct_stats.hpp"
#include "duckdb/common/string_util.hpp"

namespace duckdb {

StructFilter::StructFilter(idx_t child_idx_p, string child_name_p, unique_ptr<TableFilter> child_filter_p)
    : TableFilter(TableFilterType::STRUCT_EXTRACT), child_idx(child_idx_p), child_name(std::move(child_name_p)),
      child_filter(std::move(child_filter_p)) {
}

FilterPropagateResult StructFilter::CheckStatistics(BaseStatistics &stats) {
	D_ASSERT(stats.GetType().id() == LogicalTypeId::STRUCT);
	// Check the child statistics
	auto &child_stats = StructStats::GetChildStats(stats, child_idx);
	return child_filter->CheckStatistics(child_stats);
}

FilterPropagateResult StructFilter::CheckSketchStatistics(BaseStatistics &stats, idx_t index,
														  std::vector<std::shared_ptr<BaseColumnSketch>> &segment_sketches,
														  std::vector<ManagedSelection> &vector_sels) {
	D_ASSERT(stats.GetType().id() == LogicalTypeId::STRUCT);
	// Check the child statistics
	auto &child_stats = StructStats::GetChildStats(stats, child_idx);
	return child_filter->CheckSketchStatistics(child_stats, index, segment_sketches, vector_sels);
}

FilterPropagateResult StructFilter::CheckCubitStatistics(BaseStatistics &stats, idx_t index,
														 std::vector<std::shared_ptr<BaseCubitIndex>> &cubit_indices,
														 std::vector<ManagedSelection> &cubit_vector_sels) {
	return FilterPropagateResult::NO_PRUNING_POSSIBLE;
}

FilterPropagateResult StructFilter::CheckRabitStatistics(BaseStatistics &stats, idx_t index,
														 std::vector<std::shared_ptr<BaseRabitIndex>> &rabit_indices,
														 std::vector<ManagedSelection> &rabit_vector_sels) {
	return FilterPropagateResult::NO_PRUNING_POSSIBLE;
}

string StructFilter::ToString(const string &column_name) {
	return child_filter->ToString(column_name + "." + child_name);
}

bool StructFilter::Equals(const TableFilter &other_p) const {
	if (!TableFilter::Equals(other_p)) {
		return false;
	}
	auto &other = other_p.Cast<StructFilter>();
	return other.child_idx == child_idx && StringUtil::CIEquals(other.child_name, child_name) &&
	       other.child_filter->Equals(*child_filter);
}

} // namespace duckdb
