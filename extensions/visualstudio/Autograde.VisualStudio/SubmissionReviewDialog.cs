using System;
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
    // The file list and approval controls share one modal window, so students
    // can scroll/copy the complete snapshot before deciding whether to send it.
    internal sealed class SubmissionReviewDialog : Window
    {
        SubmissionReviewDialog(string title, string root, SubmissionSnapshot snapshot)
        {
            Title = "제출 파일 확인";
            Width = Math.Min(680, SystemParameters.WorkArea.Width - 40);
            Height = Math.Min(560, SystemParameters.WorkArea.Height - 40);
            MinWidth = Math.Min(360, Width); MinHeight = Math.Min(320, Height);
            ShowInTaskbar = false; WindowStartupLocation = WindowStartupLocation.CenterOwner;
            SetResourceReference(BackgroundProperty, EnvironmentColors.ToolWindowBackgroundBrushKey);
            SetResourceReference(ForegroundProperty, EnvironmentColors.ToolWindowTextBrushKey);
            var layout = new Grid { Margin = new Thickness(16) };
            layout.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            layout.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            layout.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            Content = layout;
            var description = new TextBox {
                Text = "선택 과제: " + title + "\n폴더: " + root + "\n" + snapshot.Files.Length + "개 파일 · " + snapshot.SourceBytes +
                    " bytes\n아래 목록을 확인하세요. 확인한 시점의 저장된 파일만 제출합니다.",
                IsReadOnly = true, TextWrapping = TextWrapping.Wrap, MaxHeight = 140,
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Margin = new Thickness(0, 0, 0, 12), Padding = new Thickness(6)
            };
            description.SetResourceReference(StyleProperty, VsResourceKeys.ThemedDialogTextBoxStyleKey);
            AutomationProperties.SetName(description, "제출할 과제와 폴더");
            layout.Children.Add(description);
            var files = new TextBox {
                Text = string.Join(Environment.NewLine, snapshot.Files), IsReadOnly = true, AcceptsReturn = true,
                TextWrapping = TextWrapping.NoWrap, VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                HorizontalScrollBarVisibility = ScrollBarVisibility.Auto, Padding = new Thickness(8)
            };
            files.SetResourceReference(StyleProperty, VsResourceKeys.ThemedDialogTextBoxStyleKey);
            AutomationProperties.SetName(files, "제출 파일 전체 목록 · 스크롤 및 복사 가능");
            Grid.SetRow(files, 1); layout.Children.Add(files);
            var actions = new WrapPanel { HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 12, 0, 0) };
            var cancel = new Button { Content = "취소", IsCancel = true, Margin = new Thickness(0, 0, 8, 0) };
            var submit = new Button { Content = "목록 확인 후 제출" };
            ThemeResources.Button(cancel); ThemeResources.Button(submit);
            submit.Click += (_, __) => DialogResult = true;
            actions.Children.Add(cancel); actions.Children.Add(submit);
            Grid.SetRow(actions, 2); layout.Children.Add(actions);
            Loaded += (_, __) => files.Focus();
        }

        public static bool Confirm(FrameworkElement owner, string title, string root, SubmissionSnapshot snapshot)
        {
            ThreadHelper.ThrowIfNotOnUIThread();
            var dialog = new SubmissionReviewDialog(title, root, snapshot);
            var window = GetWindow(owner);
            if (window != null) dialog.Owner = window;
            else if (ServiceProvider.GlobalProvider.GetService(typeof(SVsUIShell)) is IVsUIShell shell &&
                shell.GetDialogOwnerHwnd(out var handle) >= 0)
                new WindowInteropHelper(dialog).Owner = handle;
            return dialog.ShowDialog() == true;
        }
    }
}
