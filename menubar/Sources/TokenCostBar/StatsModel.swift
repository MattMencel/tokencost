import SwiftUI
import Combine

// MARK: – /stats shapes

struct HealthGrade: Codable {
    let grade: String
    let score: Int
    let color: String
    let details: [String]
    let cache_hit_rate: Double?
}

struct Summary: Codable {
    let total_cost: Double?
    let total_requests: Int?
    let total_input: Int?
    let total_cache_read: Int?
    let total_cache_creation: Int?
}

struct ActionPlan: Codable {
    let today_cost: Double?
    let monthly_projection: Double?
}

struct TaskItem: Codable {
    let category: String
    let reqs: Int
    let cost: Double?
    let one_shot_pct: Int?
}

struct TrendPoint: Codable {
    let label: String
    let cost: Double
    let reqs: Int
}

struct SessionSummary: Codable {
    let start: String?
    let total_cost: Double?
}

struct ModelItem: Codable {
    let model: String
    let reqs: Int?
    let cost: Double?
    let cache_hit_rate: Double?
    let one_shot_pct: Int?
}

struct ToolItem: Codable {
    let name: String
    let count: Int
}

struct ToolSplit: Codable {
    let core: [ToolItem]?
    let mcp: [ToolItem]?
}

struct CostBreakdown: Codable {
    let input: Double?
    let output: Double?
    let cache_read: Double?
    let cache_creation: Double?
}

struct HaikuSavings: Codable {
    let actual: Double?
    let haiku_equivalent: Double?
    let savings: Double?
    let requests: Int?
    let avg_input_tokens: Int?
    let avg_output_tokens: Int?
    let effort_counts: [String: Int]?
}

struct StatsResponse: Codable {
    let summary: Summary?
    let health_grade: HealthGrade?
    let action_plan: ActionPlan?
    let task_breakdown: [TaskItem]?
    let daily_trend: [TrendPoint]?
    let sessions: [SessionSummary]?
    let by_model: [ModelItem]?
    let tool_breakdown_split: ToolSplit?
    let cache_saved: Double?
    let cost_breakdown: CostBreakdown?
    let haiku_savings: HaikuSavings?
}

// MARK: – /projects shapes

struct ProjectItem: Codable {
    let path: String
    let cost: Double
    let sessions: Int
    let calls: Int
    let avg_per_session: Double
}

struct TopSession: Codable {
    let path: String
    let date: String
    let cost: Double
    let calls: Int
}

struct ProjectsResponse: Codable {
    let by_project: [ProjectItem]
    let top_sessions: [TopSession]
    let shell_commands: [ToolItem]
}

// MARK: – Period / Tab

enum AppPeriod: String, CaseIterable, Identifiable {
    case today = "today"
    case week  = "7d"
    case month = "30d"
    var id: String { rawValue }
    var label: String {
        switch self { case .today: "Today"; case .week: "7 Days"; case .month: "30 Days" }
    }
    var days: Double {
        switch self { case .today: 1; case .week: 7; case .month: 30 }
    }
}

enum AppTab: String, CaseIterable, Identifiable {
    case trend     = "Trend"
    case tasks     = "Tasks"
    case models    = "Models"
    case projects  = "Projects"
    case cache     = "Cache"
    case tools     = "Tools"
    case optimizer = "Optimizer"
    case logs      = "Logs"
    var id: String { rawValue }
}

// MARK: – Version shapes

struct VersionInfo: Codable {
    let current:    String
    let latest:     String?
    let up_to_date: Bool
    let update_cmd: String?
}

struct UpdateStatus: Codable {
    let running: Bool
    let result:  UpdateResult?
}

struct UpdateResult: Codable {
    let ok:      Bool
    let version: String?
    let error:   String?
}

// MARK: – Model

@MainActor
class StatsModel: ObservableObject {
    // Overview
    @Published var menuLabel    = "$—"
    @Published var todayCost    = 0.0
    @Published var periodCost   = 0.0
    @Published var monthProj    = 0.0
    @Published var totalReqs    = 0
    @Published var sessionCount = 0
    @Published var cacheHit     = 0.0
    @Published var grade        = "—"
    @Published var gradeColor   = Color.secondary
    @Published var gradeScore   = 0
    @Published var gradeDetails: [String] = []
    @Published var proxyOK      = true
    @Published var isSyncing    = false
    @Published var lastSyncAgo  = ""
    @Published var syncResult   = ""
    // Navigation
    @Published var period       = AppPeriod.today
    @Published var tab          = AppTab.trend
    // Trend
    @Published var trendData:    [TrendPoint] = []
    @Published var activityData: [TaskItem]   = []
    @Published var avgPerDay    = 0.0
    @Published var peakCost     = 0.0
    @Published var comparison   = ""
    @Published var comparisonPositive = false
    // Models
    @Published var byModel: [ModelItem] = []
    // Tools
    @Published var coreTools: [ToolItem] = []
    @Published var mcpServers: [ToolItem] = []
    // Cache
    @Published var cacheSaved     = 0.0
    @Published var cacheWriteCost = 0.0
    @Published var cacheReadCost  = 0.0
    @Published var outputCost     = 0.0
    @Published var inputCost      = 0.0
    @Published var haikuSavings   = 0.0
    @Published var haikuEquiv     = 0.0
    // Optimizer
    @Published var routingRequests   = 0
    @Published var routingActualCost = 0.0
    @Published var routingSaved      = 0.0
    @Published var routingAvgIn      = 0
    @Published var routingAvgOut     = 0
    @Published var effortCounts: [String: Int] = [:]
    // Projects
    @Published var byProject:   [ProjectItem] = []
    @Published var topSessions: [TopSession]  = []
    // Version
    @Published var currentVersion  = "—"
    @Published var latestVersion:  String? = nil
    @Published var versionUpToDate = true
    @Published var updateCmd       = ""
    @Published var isUpdating      = false
    @Published var updateResult:   String? = nil

    private var timer: Timer?

    init() {
        Task { await refreshAll() }
        Task { await fetchVersion() }
        timer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            Task { await self?.refreshAll() }
        }
    }

    func setPeriod(_ p: AppPeriod) { period = p; Task { await refreshAll() } }
    func setTab(_ t: AppTab)       { tab = t }
    func refresh()                 { Task { await refreshAll() } }

    func updateNow() {
        guard !isUpdating else { return }
        isUpdating   = true
        updateResult = nil
        Task {
            guard let url = URL(string: "http://localhost:8082/api/update") else { return }
            var req = URLRequest(url: url)
            req.httpMethod = "POST"
            req.timeoutInterval = 60
            _ = try? await URLSession.shared.data(for: req)

            // Poll for completion
            for _ in 0..<40 {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                guard let surl = URL(string: "http://localhost:8082/api/update-status") else { break }
                if let (data, _) = try? await URLSession.shared.data(from: surl),
                   let s = try? JSONDecoder().decode(UpdateStatus.self, from: data) {
                    if !s.running {
                        if s.result?.ok == true {
                            updateResult = "✓ Updated to v\(s.result?.version ?? "?")"
                            await fetchVersion()
                        } else {
                            updateResult = "✗ \(s.result?.error ?? "failed")"
                        }
                        isUpdating = false
                        return
                    }
                }
            }
            updateResult = "✗ Timeout"
            isUpdating   = false
        }
    }

    func fetchVersion() async {
        guard let url = URL(string: "http://localhost:8082/version") else { return }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            let info = try JSONDecoder().decode(VersionInfo.self, from: data)
            currentVersion  = info.current
            latestVersion   = info.latest
            versionUpToDate = info.up_to_date
            updateCmd       = info.update_cmd ?? ""
        } catch {}
    }

    func syncNow() {
        guard !isSyncing else { return }
        isSyncing = true
        syncResult = ""
        Task {
            defer { DispatchQueue.main.async { self.isSyncing = false } }
            guard let url = URL(string: "http://localhost:8082/sync-now") else { return }
            var req = URLRequest(url: url)
            req.httpMethod = "POST"
            req.timeoutInterval = 60
            do {
                let (data, _) = try await URLSession.shared.data(for: req)
                if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    let out = json["output"] as? String ?? ""
                    DispatchQueue.main.async {
                        self.syncResult = out.isEmpty ? "✓ up to date" : out
                    }
                }
                await refreshAll()
                await fetchSyncStatus()
            } catch {
                DispatchQueue.main.async { self.syncResult = "error" }
            }
        }
    }

    func fetchSyncStatus() async {
        guard let url = URL(string: "http://localhost:8082/sync-status") else { return }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let ts = json["last_import_ts"] as? String {
                let formatter = ISO8601DateFormatter()
                formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
                if let date = formatter.date(from: ts) {
                    let ago = Int(-date.timeIntervalSinceNow)
                    let agoStr = ago < 60 ? "\(ago)s ago"
                                         : ago < 3600 ? "\(ago/60)m ago"
                                         : "\(ago/3600)h ago"
                    DispatchQueue.main.async { self.lastSyncAgo = agoStr }
                }
            }
        } catch {}
    }

    func refreshAll() async {
        await withTaskGroup(of: Void.self) { group in
            group.addTask { await self.fetchStats() }
            group.addTask { await self.fetchProjects() }
            group.addTask { await self.fetchSyncStatus() }
        }
    }

    // MARK: – Fetch /stats

    private func fetchStats() async {
        guard let url = URL(string: "http://localhost:8082/stats?period=\(period.rawValue)") else { return }
        var req = URLRequest(url: url)
        req.cachePolicy = .reloadIgnoringLocalCacheData
        do {
            let (data, _) = try await URLSession.shared.data(for: req)
            let resp = try JSONDecoder().decode(StatsResponse.self, from: data)
            applyStats(resp)
            proxyOK = true
        } catch {
            proxyOK = false
            menuLabel = "$—"
        }
    }

    private func applyStats(_ r: StatsResponse) {
        todayCost   = r.action_plan?.today_cost ?? 0
        periodCost  = r.summary?.total_cost ?? 0
        monthProj   = r.action_plan?.monthly_projection ?? 0
        totalReqs   = r.summary?.total_requests ?? 0
        sessionCount = r.sessions?.count ?? 0

        let cr  = Double(r.summary?.total_cache_read    ?? 0)
        let inp = Double(r.summary?.total_input         ?? 0)
        let cw  = Double(r.summary?.total_cache_creation ?? 0)
        cacheHit = (cr + inp + cw) > 0 ? cr / (cr + inp + cw) * 100 : 0

        if let hg = r.health_grade {
            grade        = hg.grade
            gradeScore   = hg.score
            gradeDetails = hg.details
            gradeColor   = colorFromHex(hg.color)
        }

        trendData    = r.daily_trend   ?? []
        activityData = r.task_breakdown ?? []

        let days  = period.days
        avgPerDay = days > 0 ? periodCost / days : 0

        peakCost = trendData.map(\.cost).max() ?? 0

        if period != .today, avgPerDay > 0 {
            let pct = (todayCost - avgPerDay) / avgPerDay * 100
            comparisonPositive = pct >= 0
            let sign = pct >= 0 ? "↑" : "↓"
            let label = period == .week ? "7d avg" : "30d avg"
            comparison = String(format: "%@ %+.0f%% vs %@", sign, pct, label)
        } else {
            comparison = ""
        }

        byModel = r.by_model ?? []

        coreTools = r.tool_breakdown_split?.core ?? []
        mcpServers = r.tool_breakdown_split?.mcp  ?? []

        cacheSaved     = r.cache_saved ?? 0
        cacheWriteCost = r.cost_breakdown?.cache_creation ?? 0
        cacheReadCost  = r.cost_breakdown?.cache_read ?? 0
        outputCost     = r.cost_breakdown?.output ?? 0
        inputCost      = r.cost_breakdown?.input ?? 0
        haikuEquiv     = r.haiku_savings?.haiku_equivalent ?? 0
        haikuSavings   = r.haiku_savings?.savings ?? 0

        // Optimizer
        routingRequests   = r.haiku_savings?.requests ?? 0
        routingActualCost = r.haiku_savings?.actual ?? 0
        routingSaved      = r.haiku_savings?.savings ?? 0
        routingAvgIn      = r.haiku_savings?.avg_input_tokens ?? 0
        routingAvgOut     = r.haiku_savings?.avg_output_tokens ?? 0
        effortCounts      = r.haiku_savings?.effort_counts ?? [:]

        menuLabel = String(format: "$%.2f  %@", todayCost, grade)
    }

    // MARK: – Fetch /projects

    private func fetchProjects() async {
        guard let url = URL(string: "http://localhost:8082/projects?period=\(period.rawValue)") else { return }
        var req = URLRequest(url: url)
        req.cachePolicy = .reloadIgnoringLocalCacheData
        do {
            let (data, _) = try await URLSession.shared.data(for: req)
            let resp = try JSONDecoder().decode(ProjectsResponse.self, from: data)
            byProject   = resp.by_project
            topSessions = resp.top_sessions
        } catch {}
    }

    private func colorFromHex(_ hex: String) -> Color {
        let h = hex.hasPrefix("#") ? String(hex.dropFirst()) : hex
        guard h.count == 6, let val = UInt64(h, radix: 16) else { return .secondary }
        return Color(red:   Double((val >> 16) & 0xFF) / 255,
                     green: Double((val >>  8) & 0xFF) / 255,
                     blue:  Double( val        & 0xFF) / 255)
    }

    deinit { timer?.invalidate() }
}
