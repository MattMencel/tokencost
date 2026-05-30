import SwiftUI

@main
struct TokenCostBarApp: App {
    @StateObject private var model = StatsModel()

    var body: some Scene {
        MenuBarExtra {
            MenuBarView(model: model)
                .task { model.setPeriod(.today) }  // reset to Today and refresh on every open
        } label: {
            HStack(spacing: 4) {
                Image(systemName: "dollarsign.circle.fill")
                    .symbolRenderingMode(.monochrome)
                Text(model.menuLabel)
                    .font(.system(size: 11, weight: .semibold, design: .monospaced))
            }
        }
        .menuBarExtraStyle(.window)
    }
}
