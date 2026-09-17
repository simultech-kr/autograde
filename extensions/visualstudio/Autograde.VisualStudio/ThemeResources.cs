using System;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using Microsoft.VisualStudio.PlatformUI;
using Microsoft.VisualStudio.Shell;

namespace Autograde.VisualStudio
{
    // Dynamic resources, not snapshots of RGB values: the shell refreshes them
    // when the user changes themes, including Windows high-contrast themes.
    internal static class ThemeResources
    {
        public static void Apply(UserControl root, PasswordBox password, ListBox[] lists, params TextBox[] inputs)
        {
            root.SetResourceReference(Control.BackgroundProperty, EnvironmentColors.ToolWindowBackgroundBrushKey);
            root.SetResourceReference(Control.ForegroundProperty, EnvironmentColors.ToolWindowTextBrushKey);
            foreach (var input in inputs)
            {
                input.SetResourceReference(FrameworkElement.StyleProperty, VsResourceKeys.ThemedDialogTextBoxStyleKey);
                input.MinHeight = Math.Max(input.MinHeight, 28);
                input.Padding = new Thickness(5);
            }
            foreach (var list in lists)
            {
                list.SetResourceReference(FrameworkElement.StyleProperty, VsResourceKeys.ThemedDialogListBoxStyleKey);
                list.ItemContainerStyle = ListItemStyle();
            }
            // VSSDK has no PasswordBox style key; keep password masking and the
            // PART_ContentHost contract while styling all its states explicitly.
            password.Style = PasswordStyle();
        }

        public static void Label(TextBlock label) =>
            label.SetResourceReference(TextBlock.ForegroundProperty, EnvironmentColors.ToolWindowTextBrushKey);

        public static void Button(Button button) =>
            button.SetResourceReference(FrameworkElement.StyleProperty, VsResourceKeys.ThemedDialogButtonStyleKey);

        public static void Tabs(TabControl tabs)
        {
            tabs.SetResourceReference(Control.BackgroundProperty, EnvironmentColors.ToolWindowBackgroundBrushKey);
            tabs.SetResourceReference(Control.ForegroundProperty, EnvironmentColors.ToolWindowTextBrushKey);
            tabs.SetResourceReference(Control.BorderBrushProperty, CommonControlsColors.TextBoxBorderBrushKey);
            var style = new Style(typeof(TabItem));
            style.Setters.Add(Resource(Control.BackgroundProperty, EnvironmentColors.ToolWindowBackgroundBrushKey));
            style.Setters.Add(Resource(Control.ForegroundProperty, EnvironmentColors.ToolWindowTextBrushKey));
            style.Setters.Add(Resource(Control.BorderBrushProperty, CommonControlsColors.TextBoxBorderBrushKey));
            style.Setters.Add(new Setter(Control.BorderThicknessProperty, new Thickness(1)));
            style.Setters.Add(new Setter(Control.PaddingProperty, new Thickness(12, 7, 12, 7)));
            var header = new FrameworkElementFactory(typeof(ContentPresenter));
            header.SetValue(ContentPresenter.ContentSourceProperty, "Header");
            style.Setters.Add(new Setter(Control.TemplateProperty, BorderedTemplate(typeof(TabItem), header)));
            var selected = new Trigger { Property = TabItem.IsSelectedProperty, Value = true };
            selected.Setters.Add(Resource(Control.BackgroundProperty, TreeViewColors.SelectedItemActiveBrushKey));
            selected.Setters.Add(Resource(Control.ForegroundProperty, TreeViewColors.SelectedItemActiveTextBrushKey));
            style.Triggers.Add(selected);
            tabs.ItemContainerStyle = style;
        }

        static Setter Resource(DependencyProperty property, object key) =>
            new Setter(property, new DynamicResourceExtension(key));

        static ControlTemplate BorderedTemplate(Type target, FrameworkElementFactory content)
        {
            var border = new FrameworkElementFactory(typeof(Border));
            border.SetValue(Border.BackgroundProperty, new TemplateBindingExtension(Control.BackgroundProperty));
            border.SetValue(Border.BorderBrushProperty, new TemplateBindingExtension(Control.BorderBrushProperty));
            border.SetValue(Border.BorderThicknessProperty, new TemplateBindingExtension(Control.BorderThicknessProperty));
            border.SetValue(Border.PaddingProperty, new TemplateBindingExtension(Control.PaddingProperty));
            border.AppendChild(content);
            return new ControlTemplate(target) { VisualTree = border };
        }

        static Style PasswordStyle()
        {
            var style = new Style(typeof(PasswordBox));
            style.Setters.Add(Resource(Control.BackgroundProperty, CommonControlsColors.TextBoxBackgroundBrushKey));
            style.Setters.Add(Resource(Control.ForegroundProperty, CommonControlsColors.TextBoxTextBrushKey));
            style.Setters.Add(Resource(Control.BorderBrushProperty, CommonControlsColors.TextBoxBorderBrushKey));
            style.Setters.Add(Resource(PasswordBox.CaretBrushProperty, CommonControlsColors.TextBoxTextBrushKey));
            style.Setters.Add(new Setter(Control.BorderThicknessProperty, new Thickness(1)));
            style.Setters.Add(new Setter(Control.PaddingProperty, new Thickness(5)));
            style.Setters.Add(new Setter(FrameworkElement.MinHeightProperty, 28.0));
            var host = new FrameworkElementFactory(typeof(ScrollViewer), "PART_ContentHost");
            host.SetValue(UIElement.FocusableProperty, false);
            style.Setters.Add(new Setter(Control.TemplateProperty, BorderedTemplate(typeof(PasswordBox), host)));
            var hover = new Trigger { Property = UIElement.IsMouseOverProperty, Value = true };
            hover.Setters.Add(Resource(Control.BorderBrushProperty, CommonControlsColors.TextBoxBorderFocusedBrushKey));
            style.Triggers.Add(hover);
            var focused = new Trigger { Property = UIElement.IsKeyboardFocusWithinProperty, Value = true };
            focused.Setters.Add(Resource(Control.BackgroundProperty, CommonControlsColors.TextBoxBackgroundFocusedBrushKey));
            focused.Setters.Add(Resource(Control.ForegroundProperty, CommonControlsColors.TextBoxTextFocusedBrushKey));
            focused.Setters.Add(Resource(PasswordBox.CaretBrushProperty, CommonControlsColors.TextBoxTextFocusedBrushKey));
            focused.Setters.Add(Resource(Control.BorderBrushProperty, CommonControlsColors.TextBoxBorderFocusedBrushKey));
            focused.Setters.Add(new Setter(Control.BorderThicknessProperty, new Thickness(2)));
            style.Triggers.Add(focused);
            var disabled = new Trigger { Property = UIElement.IsEnabledProperty, Value = false };
            disabled.Setters.Add(Resource(Control.BackgroundProperty, CommonControlsColors.TextBoxBackgroundDisabledBrushKey));
            disabled.Setters.Add(Resource(Control.ForegroundProperty, CommonControlsColors.TextBoxTextDisabledBrushKey));
            disabled.Setters.Add(Resource(Control.BorderBrushProperty, CommonControlsColors.TextBoxBorderDisabledBrushKey));
            style.Triggers.Add(disabled);
            return style;
        }

        static Style ListItemStyle()
        {
            var style = new Style(typeof(ListBoxItem));
            style.Setters.Add(Resource(Control.BackgroundProperty, TreeViewColors.BackgroundBrushKey));
            style.Setters.Add(Resource(Control.ForegroundProperty, TreeViewColors.BackgroundTextBrushKey));
            style.Setters.Add(Resource(Control.BorderBrushProperty, TreeViewColors.BackgroundBrushKey));
            style.Setters.Add(new Setter(Control.BorderThicknessProperty, new Thickness(1)));
            style.Setters.Add(new Setter(Control.PaddingProperty, new Thickness(6, 4, 6, 4)));
            style.Setters.Add(new Setter(Control.HorizontalContentAlignmentProperty, HorizontalAlignment.Stretch));
            var content = new FrameworkElementFactory(typeof(ContentPresenter));
            content.SetValue(ContentPresenter.ContentProperty, new TemplateBindingExtension(ContentControl.ContentProperty));
            content.SetValue(ContentPresenter.ContentTemplateProperty, new TemplateBindingExtension(ContentControl.ContentTemplateProperty));
            content.SetValue(ContentPresenter.ContentTemplateSelectorProperty, new TemplateBindingExtension(ContentControl.ContentTemplateSelectorProperty));
            content.SetValue(ContentPresenter.ContentStringFormatProperty, new TemplateBindingExtension(ContentControl.ContentStringFormatProperty));
            style.Setters.Add(new Setter(Control.TemplateProperty, BorderedTemplate(typeof(ListBoxItem), content)));
            var hover = new Trigger { Property = UIElement.IsMouseOverProperty, Value = true };
            hover.Setters.Add(Resource(Control.BorderBrushProperty, TreeViewColors.FocusVisualBorderBrushKey));
            style.Triggers.Add(hover);
            // Selected text must change with its background, even after focus
            // moves from the list to the Download or Submit button.
            var selected = new Trigger { Property = ListBoxItem.IsSelectedProperty, Value = true };
            selected.Setters.Add(Resource(Control.BackgroundProperty, TreeViewColors.SelectedItemInactiveBrushKey));
            selected.Setters.Add(Resource(Control.ForegroundProperty, TreeViewColors.SelectedItemInactiveTextBrushKey));
            style.Triggers.Add(selected);
            var active = new MultiTrigger();
            active.Conditions.Add(new Condition(ListBoxItem.IsSelectedProperty, true));
            active.Conditions.Add(new Condition(Selector.IsSelectionActiveProperty, true));
            active.Setters.Add(Resource(Control.BackgroundProperty, TreeViewColors.SelectedItemActiveBrushKey));
            active.Setters.Add(Resource(Control.ForegroundProperty, TreeViewColors.SelectedItemActiveTextBrushKey));
            style.Triggers.Add(active);
            var focus = new Trigger { Property = UIElement.IsKeyboardFocusWithinProperty, Value = true };
            focus.Setters.Add(Resource(Control.BorderBrushProperty, TreeViewColors.FocusVisualBorderBrushKey));
            style.Triggers.Add(focus);
            var disabled = new Trigger { Property = UIElement.IsEnabledProperty, Value = false };
            disabled.Setters.Add(Resource(Control.BackgroundProperty, CommonControlsColors.TextBoxBackgroundDisabledBrushKey));
            disabled.Setters.Add(Resource(Control.ForegroundProperty, CommonControlsColors.TextBoxTextDisabledBrushKey));
            disabled.Setters.Add(Resource(Control.BorderBrushProperty, CommonControlsColors.TextBoxBorderDisabledBrushKey));
            style.Triggers.Add(disabled);
            return style;
        }
    }
}
