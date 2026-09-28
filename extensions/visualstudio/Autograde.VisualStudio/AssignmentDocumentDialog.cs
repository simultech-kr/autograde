using System;
using System.Linq;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Interop;
using Autograde.Core;
using Microsoft.VisualStudio.PlatformUI;
using Microsoft.VisualStudio.Shell;
using Microsoft.VisualStudio.Shell.Interop;

namespace Autograde.VisualStudio
{
    // Intentionally plain text, not a WebBrowser/Markdown renderer. Server content cannot
    // execute HTML, load images, follow links, or create files included in source bundles.
    internal sealed class AssignmentDocumentDialog : Window
    {
        AssignmentDocumentDialog(string title, AssignmentDocument document)
        {
            Title = "과제 설명 v" + document.Metadata.Revision + " · 읽기 전용";
            Width = Math.Min(820, SystemParameters.WorkArea.Width - 40);
            Height = Math.Min(720, SystemParameters.WorkArea.Height - 40);
            MinWidth = Math.Min(360, Width); MinHeight = Math.Min(320, Height);
            ShowInTaskbar = false; WindowStartupLocation = WindowStartupLocation.CenterOwner;
            SetResourceReference(BackgroundProperty, EnvironmentColors.ToolWindowBackgroundBrushKey);
            SetResourceReference(ForegroundProperty, EnvironmentColors.ToolWindowTextBrushKey);
            var layout = new Grid { Margin = new Thickness(16) }; Content = layout;
            layout.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            layout.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            layout.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            layout.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            var heading = Text("선택 과제: " + title + "\n설명 v" + document.Metadata.Revision + " · " + document.Metadata.UpdatedAt +
                "\n변경 안내: " + document.Metadata.ChangeNote + "\n읽기 전용 원문입니다. README·답안·제출 파일은 변경하지 않습니다.", "과제 설명 버전과 변경 안내");
            heading.MaxHeight = 140; layout.Children.Add(heading);
            var body = Text(document.Content, "과제 설명 Markdown 원문 · 읽기 전용 · 스크롤 및 복사 가능");
            body.Margin = new Thickness(0, 10, 0, 10); Grid.SetRow(body, 1); layout.Children.Add(body);
            var versions = Text(string.Join(Environment.NewLine, document.History.Select(item =>
                "v" + item.Revision + " · " + item.UpdatedAt + " · " + item.ChangeNote)), "최근 과제 설명 변경 이력");
            versions.MaxHeight = 130;
            var history = new Expander { Header = "최근 변경 이력 (최대 50개)", Content = versions };
            Grid.SetRow(history, 2); layout.Children.Add(history);
            var close = new Button { Content = "닫기", IsCancel = true, HorizontalAlignment = HorizontalAlignment.Right,
                Margin = new Thickness(0, 10, 0, 0) };
            ThemeResources.Button(close); Grid.SetRow(close, 3); layout.Children.Add(close);
            Loaded += (_, __) => body.Focus();
        }

        static TextBox Text(string value, string name)
        {
            var text = new TextBox { Text = value, IsReadOnly = true, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap,
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Padding = new Thickness(6) };
            text.SetResourceReference(StyleProperty, VsResourceKeys.ThemedDialogTextBoxStyleKey);
            AutomationProperties.SetName(text, name);
            return text;
        }

        public static void Open(FrameworkElement owner, string title, AssignmentDocument document)
        {
            ThreadHelper.ThrowIfNotOnUIThread();
            var dialog = new AssignmentDocumentDialog(title, document);
            var window = GetWindow(owner);
            if (window != null) dialog.Owner = window;
            else if (ServiceProvider.GlobalProvider.GetService(typeof(SVsUIShell)) is IVsUIShell shell &&
                shell.GetDialogOwnerHwnd(out var handle) >= 0)
                new WindowInteropHelper(dialog).Owner = handle;
            dialog.ShowDialog();
        }
    }
}
