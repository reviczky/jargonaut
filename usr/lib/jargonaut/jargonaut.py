#!/usr/bin/python3

import gi
import irc.client
gi.require_version('Gtk', '3.0')
gi.require_version('XApp', '1.0')
gi.require_version('WebKit2', '4.1')
from gi.repository import Gtk, Gio, GLib, Gdk, XApp, WebKit2
from irc.connection import Factory
import random
import threading
import getpass
import random
import re
import ssl
import gettext
import locale
import setproctitle
from settings import bind_entry_widget, bind_switch_widget
from ui import build_menu, idle, _async, color_palette
setproctitle.setproctitle("jargonaut")

# i18n
APP = "jargonaut"
LOCALE_DIR = "/usr/share/locale"
locale.bindtextdomain(APP, LOCALE_DIR)
gettext.bindtextdomain(APP, LOCALE_DIR)
gettext.textdomain(APP)
_ = gettext.gettext

class Message():
    def __init__(self, nick, text):
        self.nick = nick
        self.text = text
class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.x.jargonaut")
        self.window = None

    def do_activate(self):
        # If the window already exists, present it to the user
        if self.window is not None:
            self.window.present()
            return

        self.is_connected = False
        self.user_colors = {}
        self.color_index = 0

        self.settings = Gio.Settings(schema="org.x.jargonaut")
        self.channel = self.settings.get_string("channel")
        self.server = self.settings.get_string("server")
        self.port = self.settings.get_int("port")
        self.tls = self.settings.get_boolean("tls-connection")
        self.nickname = self.get_new_nickname()

        self.channel_users = {}
        self.channel_users[self.channel] = []
        self.messages = []

        prefer_dark_mode = self.settings.get_boolean("prefer-dark-mode")
        try:
            # DarkModeManager is available in XApp 2.6+
            self.dark_mode_manager = XApp.DarkModeManager.new(prefer_dark_mode)
        except:
            print("XAppDarkModeManager not available, using Gtk.Settings")
            # Use the Gtk.Settings API as a fallback for older versions
            Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", prefer_dark_mode)

        self.last_key_press_is_tab = False
        self.last_message_nick = ""

        self.builder = Gtk.Builder()
        self.builder.add_from_file("/usr/share/jargonaut/jargonaut.ui")
        self.window = self.builder.get_object("main_window")
        self.window.set_application(self)
        self.window.show_all()

        css_provider = Gtk.CssProvider()
        css_provider.load_from_path("/usr/share/jargonaut/style.css")
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        # Menubar
        menu = self.builder.get_object("main_menu")
        build_menu(self, self.window, menu)

        # Settings widgets
        bind_entry_widget(self.builder.get_object("pref_nickname"), self.settings, "nickname")
        bind_entry_widget(self.builder.get_object("pref_password"), self.settings, "password")
        bind_switch_widget(self.builder.get_object("pref_dark"), self.settings, "prefer-dark-mode", fn_callback=self.update_dark_mode)

        self.treeview = self.builder.get_object("treeview_chat")
        self.store = Gtk.ListStore(str, str, str) # nick, message
        self.treeview.set_model(self.store)

        self.webview = WebKit2.WebView()
        self.builder.get_object("page_discussion").pack_start(self.webview, True, True, 0)
        self.webview.show()

        renderer = Gtk.CellRendererText()
        renderer.props.xalign = 1.0
        col = Gtk.TreeViewColumn("", renderer, markup=0)
        col.set_name("first_column")
        self.treeview.append_column(col)

        # Add a dedicated column which always renders |
        renderer = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn("", renderer, text=2)
        self.treeview.append_column(col)

        renderer = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn("", renderer, markup=1)
        self.treeview.append_column(col)

        self.user_treeview = self.builder.get_object("treeview_users")
        self.user_store = Gtk.ListStore(str, str) # nick, raw_nick
        self.user_treeview.set_model(self.user_store)
        self.user_store.set_sort_column_id(1, Gtk.SortType.ASCENDING)

        completion = Gtk.EntryCompletion()
        completion.set_model(self.user_store)
        completion.set_text_column(1)
        completion.set_inline_completion(True)
        completion.set_popup_completion(True)

        self.entry = self.builder.get_object("entry_main")
        self.entry.set_completion(completion)
        self.entry.connect("key-press-event", self.on_key_press_event)

        renderer = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn("Users", renderer, markup=0)
        self.user_treeview.append_column(col)

        self.assign_color(self.nickname)
        self.builder.get_object("label_username").set_markup(self.get_nick_markup(self.nickname))

        self.builder.get_object("channel_stack").connect("notify::visible-child-name", self.on_page_changed)

        self.client = irc.client.SimpleIRCClient()
        self.client.connection.add_global_handler("welcome", self.on_welcome)
        self.client.connection.add_global_handler("join", self.on_join)
        self.client.connection.add_global_handler("namreply", self.on_namreply)
        self.client.connection.add_global_handler("notice", self.on_notice)
        self.client.connection.add_global_handler("nick", self.on_nick)
        self.client.connection.add_global_handler("quit", self.on_quit)
        self.client.connection.add_global_handler("part", self.on_part)
        self.client.connection.add_global_handler("all_raw_messages", self.on_all_raw_messages)
        self.client.connection.add_global_handler("pubmsg", self.on_pubmsg)
        self.client.connection.add_global_handler("erroneusnickname", self.on_erroneusnickname)
        self.client.connection.add_global_handler("disconnect", self.on_disconnect)
        self.client.connection.add_global_handler("error", self.on_error)
        self.client.connection.add_global_handler("nicknameinuse", self.on_nicknameinuse)
        self.connect_to_server()

#########################
# Nickname/user functions
#########################

    def assign_color(self, nick):
        if nick not in self.user_colors.keys():
            color = color_palette[self.color_index]
            print(f"Assigning color {color} to {nick}")
            self.user_colors[nick] = color
            self.color_index = (self.color_index + 1) % len(color_palette)

    def get_nick_markup(self, nick):
        if nick == "":
            color = "grey"
        elif nick == "*":
            color = "red"
        else:
            color = self.user_colors[nick]
        nick = f"<span foreground='{color}'>{nick}</span>"
        return nick

    def get_new_nickname(self, with_random_suffix=False):
        if self.settings.get_string("nickname") != "":
            prefix = self.settings.get_string("nickname")
        else:
            prefix = getpass.getuser()
        if with_random_suffix:
            prefix = prefix[:13] # 13 chars max + 3 chars for suffix
            suffix = '{:02x}'.format(random.randint(0, 255))
            return f"{prefix}_{suffix}"
        else:
            return prefix[:16] # 16 chars max

#####################
# IRC commands
#####################

    @_async
    def connect_to_server(self):
        try:
            if self.tls:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                factory = Factory(wrapper=context.wrap_socket)
                self.client.connect(self.settings.get_string("server"),
                                    self.settings.get_int("port"),
                                    self.nickname, connect_factory=factory)
            else:
                self.client.connect(self.settings.get_string("server"),
                                    self.settings.get_int("port"),
                                    self.nickname)
            self.is_connected = True
            self.client.start()
        except Exception as e:
            self.show_error_status("dialog-error-symbolic", _("Error"), str(e))

    @_async
    def join_channels(self, connection):
        connection.join(self.channel)
        connection.names([self.channel])

    @_async
    def identify(self, connection):
        username = self.settings.get_string("nickname")
        password = self.settings.get_string("password")
        if password != "":
            self.print_info("Identifying...")
            connection.privmsg("Nickserv", f"IDENTIFY {username} {password}")

    @_async
    def send_message(self, message):
        message = message.replace('\x16', '\x1D')
        self.client.connection.privmsg(self.channel, message)

    @_async
    def disconnect(self):
        try:
            self.client.connection.disconnect(message=_("Jargonaut signing out!"))
        except Exception as e:
            print(e)

#####################
# IRC signal handlers
#####################

    @idle
    def on_welcome(self, connection, event):
        self.join_channels(connection)

    @idle
    def on_join(self, connection, event):
        self.print_info(f"Joined channel: target={event.target} source={event.source}")
        self.builder.get_object("main_stack").set_visible_child_name("page_chat")
        nick = event.source.nick
        channel = event.target
        if nick not in self.channel_users[channel]:
            self.assign_color(nick)
            self.channel_users[channel].append(nick)
            self.update_users()
        if nick == self.nickname:
            self.identify(connection)

    @idle
    def on_namreply(self, connection, event):
        channel = event.arguments[1]
        users = event.arguments[2].split()
        for user in users:
            for character in ["~", "&", "@", "%", "+"]:
                if user.startswith(character):
                    user = user[1:]
                    break
            self.assign_color(user)
            if not user in self.channel_users[channel]:
                self.channel_users[channel].append(user)
        self.update_users()

    @idle
    def on_notice(self, connection, event):
        self.print_info("Notice: " + event.source + " " + event.arguments[0])

    @idle
    def on_nick(self, connection, event):
        self.print_info(f"Nick: target={event.target} source={event.source}")
        old_nick = event.source.nick
        new_nick = event.target
        if old_nick in self.channel_users[self.channel]:
            self.channel_users[self.channel].remove(old_nick)
        if not new_nick in self.channel_users[self.channel]:
            self.channel_users[self.channel].append(new_nick)
        if not new_nick in self.user_colors.keys():
            self.assign_color(new_nick)
        if old_nick == self.nickname:
            self.nickname = new_nick
            self.builder.get_object("label_username").set_markup(self.get_nick_markup(new_nick))
        self.update_users()

    @idle
    def on_quit(self, connection, event):
        self.print_info(f"Quit: target={event.target} source={event.source}")
        nick = event.source.nick
        channel = event.target
        if nick in self.channel_users[self.channel]:
            self.channel_users[self.channel].remove(nick)
            self.update_users()

    @idle
    def on_part(self, connection, event):
        self.print_info(f"Part: target={event.target} source={event.source}")
        nick = event.source.nick
        channel = event.target
        if nick in self.channel_users[channel]:
            self.channel_users[channel].remove(nick)
            self.update_users()

    @idle
    def on_all_raw_messages(self, connection, event):
        if self.settings.get_boolean("debug"):
            self.print_info("Raw message: " + str(event.arguments))

    @idle
    def on_pubmsg(self, connection, event):
        nick = event.source.split('!')[0]
        message = event.arguments[0]
        self.print_message(nick, message)

    @idle
    def on_erroneusnickname(self, connection, event):
        self.print_info("Invalid nickname", event.arguments[0])
        self.show_error_status("dialog-error-symbolic", _("Invalid nickname"), _("Your nickname was rejected. Restart the application to reset it."))
        self.settings.set_string("nickname", "")

    @idle
    def on_disconnect(self, connection, event):
        self.print_info("Disconnected from server: ", event.target)
        self.show_error_status("dialog-error-symbolic", _("Disconnected"), _("You have been disconnected from the server. Please try to reconnect."))

    @idle
    def on_error(self, connection, event):
        self.print_info("Error from server: ", event.arguments[0])
        self.show_error_status("dialog-error-symbolic", _("Error"), _("An error occurred: ") + event.arguments[0])

    @idle
    def on_nicknameinuse(self, connection, event):
        self.nickname =  self.get_new_nickname(with_random_suffix=True)
        self.assign_color(self.nickname)
        connection.nick(self.nickname)
        self.print_info(f"Nickname in use, switching to '{self.nickname}'")
        self.builder.get_object("label_username").set_markup(self.get_nick_markup(self.nickname))

##################
# UI IRC functions
##################

    def render_html(self):
        messages_section = ""
        last_nick = ""
        for message in self.messages:
            class_name = "theirs"
            nickname = message.nick
            color = self.user_colors[nickname]
            nickname = f"<font color='{color}'>{nickname}</font>"
            sep = "|"
            text = message.text
            words = text.lower().split(" ")
            if message.nick == self.nickname:
                nickname = "*"
                sep = ">"
                class_name = "mine"
            elif self.nickname.lower() in words or (self.nickname+":").lower() in words or ("@"+self.nickname).lower() in words:
                class_name = "response"
            if message.nick == last_nick:
                nickname = ""
            messages_section += f"""
                    <tr class="{class_name}" valign="top">
                        <td class="nick">{nickname}</td>
                        <td class="sep">{sep}</td>
                        <td class="msg">{text}</td>
                    </tr>
                    """
            last_nick = message.nick

        html = f"""
<html>
<head>
    <link rel="stylesheet" type="text/css" href="webview.css">
</head>
<body>
    <table class="chattable">
        {messages_section}
    </table>
    <script>
        window.scrollTo(0, document.body.scrollHeight);
    </script>
</body>
</html>
        """

        self.webview.load_html(html, "file:///usr/share/jargonaut/")

    def render_treeview(self, message):
        text = message.text
        sep = "|"
        nickname = message.nick
        words = text.lower().split(" ")
        if message.nick == self.nickname:
            nickname = "*"
            sep = ">"
            text = f"<span foreground='grey'>{text}</span>"
        elif self.nickname.lower() in words or (self.nickname+":").lower() in words or ("@"+self.nickname).lower() in words:
            text = f"<span foreground='red'>{text}</span>"
        if message.nick == self.last_message_nick:
            nickname = ""
        iter = self.store.append([self.get_nick_markup(nickname), text, sep])
        path = self.store.get_path(iter)
        self.treeview.scroll_to_cell(path, None, False, 0.0, 0.0)

    @idle
    def print_message(self, nick, text):
        # Format text (IRC codes -> pango/HTML)
        text = re.sub(r'\x02(.*?)\x02', r'<b>\1</b>', text)
        text = re.sub(r'\x16(.*?)\x16', r'<i>\1</i>', text)
        text = re.sub(r'\x1D(.*?)\x1D', r'<i>\1</i>', text)
        text = re.sub(r'\x1F(.*?)\x1F', r'<u>\1</u>', text)
        text = re.sub(r'\x1E(.*?)\x1E', r'<s>\1</s>', text)
        message = Message(nick, text)
        self.messages.append(message)
        self.render_treeview(message)
        self.render_html()
        self.last_message_nick = nick

    @idle
    def update_users(self):
        self.user_store.clear()
        users = self.channel_users[self.channel]
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        icon = Gtk.Image.new_from_icon_name("system-users-symbolic", Gtk.IconSize.MENU)
        box.pack_start(icon, True, True, 0)
        box.pack_start(Gtk.Label(label=len(users)), True, True, 0)
        column = self.user_treeview.get_column(0)
        column.set_widget(box)
        column.set_alignment(0.5)
        box.show_all()
        for user in users:
            self.user_store.append([self.get_nick_markup(user), user])

    @idle
    def print_info(self, message):
        print("Info: " + message)

    @idle
    def show_error_status(self, icon_name, message, details):
        self.builder.get_object("main_stack").set_visible_child_name("page_status")
        self.builder.get_object("status_icon").set_from_icon_name(icon_name, Gtk.IconSize.DIALOG)
        self.builder.get_object("status_label").set_text(message)
        self.builder.get_object("status_details").set_text(details)

###############
# App functions
###############

    def on_page_changed(self, stack, param):
        if stack.get_visible_child_name() in ["page_questions", "page_discussion"]:
            self.builder.get_object("entry_box").show_all()
            GLib.idle_add(self.entry.grab_focus)
        else:
            self.builder.get_object("entry_box").set_visible(False)

    @idle
    def update_dark_mode(self, active):
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", active)

    def on_key_press_event(self, widget, event):
        keyname = Gdk.keyval_name(event.keyval)
        ctrl = (event.state & Gdk.ModifierType.CONTROL_MASK)
        position = widget.get_position()
        text = widget.get_text()
        if keyname == "Tab":
            if " " in text:
                return True
            if text == "":
                return True
            completion = widget.get_completion()
            model = completion.get_model()
            if len(model) > 0:
                num_matches = 0
                for row in model:
                    if row[1].startswith(text):
                        num_matches += 1
                if num_matches > 0:
                    # Adding prefix
                    completion.insert_prefix()
                    if num_matches == 1:
                        # Adding suffix (cause single match)
                        widget.set_text(text + ": ")
                    elif self.last_key_press_is_tab:
                        # Adding suffix (cause double tab)
                        widget.set_text(text + ": ")
                widget.set_position(-1)
                self.last_key_press_is_tab = True
            return True  # Stop propagation of the event
        else:
            self.last_key_press_is_tab = False
        if keyname == "Return":
            message = text.strip()
            if message != "":
                completion = widget.get_completion()
                widget.set_completion(None)
                widget.set_text("")
                widget.set_completion(completion)
                self.print_message(self.nickname, message)
                self.send_message(message)
            return True
        elif ctrl and keyname == "b":
            widget.set_text(text + "\x02")
            widget.set_position(position + 1)
            return True
        elif ctrl and keyname == "i":
            # This should be \x1D but Gtk.Entry won't accept it
            # so we use \x16 instead and we replace it when sending
            # the message
            widget.set_text(text + "\x16")
            widget.set_position(position + 1)
            return True
        elif ctrl and keyname == "u":
            widget.set_text(text + "\x1F")
            widget.set_position(position + 1)
            return True
        elif ctrl and keyname == "s":
            widget.set_text(text + "\x1E")
            widget.set_position(position + 1)
            return True

        return False

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.connect("shutdown", self.on_shutdown)

    def on_shutdown(self, application):
        if self.is_connected:
            self.disconnect()

app = App()
app.run()