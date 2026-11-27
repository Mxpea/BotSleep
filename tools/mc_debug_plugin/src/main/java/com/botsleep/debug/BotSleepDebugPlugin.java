package com.botsleep.debug;

import org.bukkit.Bukkit;
import org.bukkit.ChatColor;
import org.bukkit.GameMode;
import org.bukkit.Location;
import org.bukkit.Material;
import org.bukkit.Particle;
import org.bukkit.World;
import org.bukkit.block.Block;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Entity;
import org.bukkit.entity.Monster;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.block.BlockBreakEvent;
import org.bukkit.event.block.BlockPlaceEvent;
import org.bukkit.event.entity.EntityDamageByEntityEvent;
import org.bukkit.event.entity.EntityDamageEvent;
import org.bukkit.event.entity.PlayerDeathEvent;
import org.bukkit.event.inventory.InventoryOpenEvent;
import org.bukkit.event.inventory.InventoryType;
import org.bukkit.event.player.PlayerAdvancementDoneEvent;
import org.bukkit.event.player.PlayerBucketEmptyEvent;
import org.bukkit.event.player.PlayerBucketFillEvent;
import org.bukkit.event.player.PlayerKickEvent;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.event.player.PlayerRespawnEvent;
import org.bukkit.inventory.ItemStack;
import org.bukkit.inventory.meta.Damageable;
import org.bukkit.inventory.meta.ItemMeta;
import org.bukkit.plugin.java.JavaPlugin;
import org.bukkit.util.Vector;

import java.io.BufferedWriter;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TimeZone;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;

public final class BotSleepDebugPlugin extends JavaPlugin implements Listener {

    private final LinkedBlockingQueue<String> writeQueue = new LinkedBlockingQueue<>(32768);
    private final AtomicBoolean writerRunning = new AtomicBoolean(false);
    private final AtomicLong totalEvents = new AtomicLong(0);
    private final AtomicLong droppedEvents = new AtomicLong(0);
    private final Object writerLock = new Object();

    private final Map<String, String> lastSampleCell = new ConcurrentHashMap<>();
    private final Map<String, Map<String, Integer>> hotStuck = new ConcurrentHashMap<>();
    private final Set<String> markedPlayers = ConcurrentHashMap.newKeySet();

    private volatile String activeRunId = "default";
    private volatile Path activeLogPath = null;
    private BufferedWriter activeWriter = null;
    private Thread writerThread = null;
    private long serverTicks = 0L;

    @Override
    public void onEnable() {
        SimpleDateFormat fmt = new SimpleDateFormat("yyyyMMdd_HHmmss", Locale.ROOT);
        fmt.setTimeZone(TimeZone.getTimeZone("UTC"));
        String defaultRun = "session_" + fmt.format(new Date());
        rotateRun(defaultRun);

        writerRunning.set(true);
        writerThread = new Thread(this::writerLoop, "BotSleepDebug-Writer");
        writerThread.setDaemon(true);
        writerThread.start();

        getServer().getPluginManager().registerEvents(this, this);
        getServer().getScheduler().runTaskTimer(this, this::onSampleTick, 10L, 10L);
        getLogger().info("BotSleepDebug enabled; logging server_events.jsonl to " + activeLogPath);
    }

    @Override
    public void onDisable() {
        writerRunning.set(false);
        if (writerThread != null) {
            try {
                writerThread.join(2500L);
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            }
        }
        drainAndFlush();
        closeWriter();
    }

    private void rotateRun(String rawRunId) {
        String sanitized = rawRunId == null ? "default" : rawRunId.replaceAll("[^A-Za-z0-9_.-]", "_");
        if (sanitized.isEmpty()) {
            sanitized = "default";
        }
        Path runDir = getDataFolder().toPath().resolve("runs").resolve(sanitized);
        Path target = runDir.resolve("server_events.jsonl");
        synchronized (writerLock) {
            drainAndFlushLocked();
            closeWriterLocked();
            try {
                Files.createDirectories(runDir);
                activeWriter = Files.newBufferedWriter(
                    target,
                    StandardCharsets.UTF_8,
                    StandardOpenOption.CREATE,
                    StandardOpenOption.APPEND
                );
                activeRunId = sanitized;
                activeLogPath = target;
            } catch (IOException ex) {
                getLogger().warning("Failed to open debug log at " + target + ": " + ex.getMessage());
            }
        }
    }

    private void writerLoop() {
        List<String> batch = new ArrayList<>(32);
        while (writerRunning.get() || !writeQueue.isEmpty()) {
            try {
                String first = writeQueue.poll(400L, TimeUnit.MILLISECONDS);
                if (first != null) {
                    batch.add(first);
                    writeQueue.drainTo(batch, 31);
                    synchronized (writerLock) {
                        if (activeWriter != null) {
                            for (String line : batch) {
                                activeWriter.write(line);
                                activeWriter.newLine();
                            }
                            activeWriter.flush();
                        }
                    }
                    batch.clear();
                }
            } catch (InterruptedException ignored) {
                if (!writerRunning.get()) {
                    break;
                }
            } catch (IOException ex) {
                getLogger().warning("BotSleepDebug write error: " + ex.getMessage());
                batch.clear();
            }
        }
    }

    private void drainAndFlush() {
        synchronized (writerLock) {
            drainAndFlushLocked();
        }
    }

    private void drainAndFlushLocked() {
        if (activeWriter == null) {
            return;
        }
        List<String> rest = new ArrayList<>();
        writeQueue.drainTo(rest);
        if (!rest.isEmpty()) {
            try {
                for (String line : rest) {
                    activeWriter.write(line);
                    activeWriter.newLine();
                }
                activeWriter.flush();
            } catch (IOException ignored) {
            }
        }
    }

    private void closeWriter() {
        synchronized (writerLock) {
            closeWriterLocked();
        }
    }

    private void closeWriterLocked() {
        if (activeWriter != null) {
            try {
                activeWriter.flush();
                activeWriter.close();
            } catch (IOException ignored) {
            }
            activeWriter = null;
        }
    }

    private void emit(String jsonLine) {
        if (writeQueue.offer(jsonLine)) {
            totalEvents.incrementAndGet();
        } else {
            droppedEvents.incrementAndGet();
        }
    }

    private static String normalizeDim(World w) {
        if (w == null) {
            return "unknown";
        }
        String name = w.getEnvironment().name();
        return switch (name) {
            case "NORMAL" -> "overworld";
            case "NETHER" -> "the_nether";
            case "THE_END" -> "the_end";
            default -> w.getName();
        };
    }

    private static String esc(String raw) {
        if (raw == null) {
            return "";
        }
        StringBuilder sb = new StringBuilder(raw.length() + 8);
        for (int i = 0; i < raw.length(); i++) {
            char c = raw.charAt(i);
            switch (c) {
                case '"' -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\b' -> sb.append("\\b");
                case '\f' -> sb.append("\\f");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
                default -> {
                    if (c < 0x20) {
                        sb.append(String.format(Locale.ROOT, "\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
                }
            }
        }
        return sb.toString();
    }

    private static int handDamage(ItemStack item) {
        if (item == null || item.getType() == Material.AIR) {
            return 0;
        }
        ItemMeta meta = item.getItemMeta();
        if (meta instanceof Damageable dmg) {
            return dmg.getDamage();
        }
        return 0;
    }

    private StringBuilder baseHeader(String eventName, String playerName, World world) {
        long nowMs = System.currentTimeMillis();
        StringBuilder sb = new StringBuilder(256);
        sb.append("{\"ts_ms\":").append(nowMs);
        sb.append(",\"ts\":").append(String.format(Locale.ROOT, "%.3f", nowMs / 1000.0));
        sb.append(",\"tick\":").append(serverTicks);
        sb.append(",\"source\":\"mc_server\"");
        sb.append(",\"event\":\"").append(esc(eventName)).append("\"");
        sb.append(",\"player\":\"").append(esc(playerName)).append("\"");
        sb.append(",\"dim\":\"").append(esc(normalizeDim(world))).append("\"");
        return sb;
    }

    private void onSampleTick() {
        serverTicks += 10L;
        for (Player p : Bukkit.getOnlinePlayers()) {
            if (p.getGameMode() == GameMode.SPECTATOR) {
                continue;
            }
            Location loc = p.getLocation();
            World w = loc.getWorld();
            if (w == null) {
                continue;
            }
            String name = p.getName();
            int bx = loc.getBlockX();
            int by = loc.getBlockY();
            int bz = loc.getBlockZ();
            String dim = normalizeDim(w);
            String cellKey = dim + ":" + bx + ":" + by + ":" + bz;
            String prev = lastSampleCell.put(name, cellKey);
            boolean sameCell = cellKey.equals(prev);
            if (sameCell) {
                hotStuck.computeIfAbsent(name, k -> new ConcurrentHashMap<>())
                    .merge(cellKey, 1, Integer::sum);
            }

            Vector vel = p.getVelocity();
            ItemStack hand = p.getInventory().getItemInMainHand();
            String handName = hand.getType().name().toLowerCase(Locale.ROOT);
            int dmg = handDamage(hand);
            int maxDur = hand.getType().getMaxDurability();

            String feetBlk = w.getBlockAt(bx, by, bz).getType().name().toLowerCase(Locale.ROOT);
            String headBlk = w.getBlockAt(bx, by + 1, bz).getType().name().toLowerCase(Locale.ROOT);
            String belowBlk = w.getBlockAt(bx, by - 1, bz).getType().name().toLowerCase(Locale.ROOT);

            int hostiles12m = 0;
            for (Entity near : p.getNearbyEntities(12.0, 8.0, 12.0)) {
                if (near instanceof Monster) {
                    hostiles12m++;
                }
            }

            StringBuilder sb = baseHeader("PLAYER_SAMPLE", name, w);
            sb.append(",\"data\":{");
            sb.append("\"pos\":[").append(String.format(Locale.ROOT, "%.2f,%.2f,%.2f", loc.getX(), loc.getY(), loc.getZ())).append("]");
            sb.append(",\"block\":[").append(bx).append(',').append(by).append(',').append(bz).append(']');
            sb.append(",\"same_cell\":").append(sameCell);
            sb.append(",\"vel\":[").append(String.format(Locale.ROOT, "%.3f,%.3f,%.3f", vel.getX(), vel.getY(), vel.getZ())).append("]");
            sb.append(",\"on_ground\":").append(p.isOnGround());
            sb.append(",\"in_water\":").append(p.isInWater());
            sb.append(",\"in_lava\":").append(p.isInLava());
            sb.append(",\"sneaking\":").append(p.isSneaking());
            sb.append(",\"hp\":").append(String.format(Locale.ROOT, "%.1f", p.getHealth()));
            sb.append(",\"food\":").append(p.getFoodLevel());
            sb.append(",\"hand\":\"").append(esc(handName)).append("\"");
            sb.append(",\"hand_dmg\":").append(dmg);
            sb.append(",\"hand_max_dur\":").append(maxDur);
            sb.append(",\"feet_block\":\"").append(esc(feetBlk)).append("\"");
            sb.append(",\"head_block\":\"").append(esc(headBlk)).append("\"");
            sb.append(",\"below_block\":\"").append(esc(belowBlk)).append("\"");
            sb.append(",\"hostiles_12m\":").append(hostiles12m);
            sb.append("}}");
            emit(sb.toString());

            if (markedPlayers.contains(name)) {
                renderPlayerMarkers(p, w);
            }
        }
    }

    private void renderPlayerMarkers(Player p, World w) {
        Map<String, Integer> map = hotStuck.get(p.getName());
        if (map == null || map.isEmpty()) {
            return;
        }
        String myDim = normalizeDim(w);
        map.entrySet().stream()
            .filter(e -> e.getValue() >= 3 && e.getKey().startsWith(myDim + ":"))
            .sorted(Map.Entry.<String, Integer>comparingByValue().reversed())
            .limit(8)
            .forEach(e -> {
                String[] parts = e.getKey().split(":");
                if (parts.length == 4) {
                    try {
                        double x = Integer.parseInt(parts[1]) + 0.5;
                        double y = Integer.parseInt(parts[2]) + 2.2;
                        double z = Integer.parseInt(parts[3]) + 0.5;
                        w.spawnParticle(Particle.FLAME, x, y, z, 2, 0.08, 0.12, 0.08, 0.0);
                    } catch (NumberFormatException ignored) {
                    }
                }
            });
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = false)
    public void onBlockBreak(BlockBreakEvent event) {
        Player p = event.getPlayer();
        Block b = event.getBlock();
        ItemStack hand = p.getInventory().getItemInMainHand();
        StringBuilder sb = baseHeader("BLOCK_BREAK", p.getName(), b.getWorld());
        sb.append(",\"data\":{");
        sb.append("\"pos\":[").append(b.getX()).append(',').append(b.getY()).append(',').append(b.getZ()).append(']');
        sb.append(",\"block\":\"").append(esc(b.getType().name().toLowerCase(Locale.ROOT))).append("\"");
        sb.append(",\"hand\":\"").append(esc(hand.getType().name().toLowerCase(Locale.ROOT))).append("\"");
        sb.append(",\"hand_dmg\":").append(handDamage(hand));
        sb.append(",\"cancelled\":").append(event.isCancelled());
        sb.append("}}");
        emit(sb.toString());
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = false)
    public void onBlockPlace(BlockPlaceEvent event) {
        Player p = event.getPlayer();
        Block b = event.getBlockPlaced();
        Block ag = event.getBlockAgainst();
        StringBuilder sb = baseHeader("BLOCK_PLACE", p.getName(), b.getWorld());
        sb.append(",\"data\":{");
        sb.append("\"pos\":[").append(b.getX()).append(',').append(b.getY()).append(',').append(b.getZ()).append(']');
        sb.append(",\"block\":\"").append(esc(b.getType().name().toLowerCase(Locale.ROOT))).append("\"");
        sb.append(",\"against\":\"").append(esc(ag.getType().name().toLowerCase(Locale.ROOT))).append("\"");
        sb.append(",\"cancelled\":").append(event.isCancelled());
        sb.append("}}");
        emit(sb.toString());
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = false)
    public void onBucketFill(PlayerBucketFillEvent event) {
        Player p = event.getPlayer();
        Block b = event.getBlock();
        StringBuilder sb = baseHeader("BUCKET_FILL", p.getName(), b.getWorld());
        sb.append(",\"data\":{");
        sb.append("\"pos\":[").append(b.getX()).append(',').append(b.getY()).append(',').append(b.getZ()).append(']');
        sb.append(",\"bucket\":\"").append(esc(event.getBucket().name().toLowerCase(Locale.ROOT))).append("\"");
        sb.append(",\"block\":\"").append(esc(b.getType().name().toLowerCase(Locale.ROOT))).append("\"");
        sb.append(",\"cancelled\":").append(event.isCancelled());
        sb.append("}}");
        emit(sb.toString());
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = false)
    public void onBucketEmpty(PlayerBucketEmptyEvent event) {
        Player p = event.getPlayer();
        Block b = event.getBlock();
        StringBuilder sb = baseHeader("BUCKET_EMPTY", p.getName(), b.getWorld());
        sb.append(",\"data\":{");
        sb.append("\"pos\":[").append(b.getX()).append(',').append(b.getY()).append(',').append(b.getZ()).append(']');
        sb.append(",\"bucket\":\"").append(esc(event.getBucket().name().toLowerCase(Locale.ROOT))).append("\"");
        sb.append(",\"block\":\"").append(esc(b.getType().name().toLowerCase(Locale.ROOT))).append("\"");
        sb.append(",\"cancelled\":").append(event.isCancelled());
        sb.append("}}");
        emit(sb.toString());
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onPlayerDamage(EntityDamageEvent event) {
        if (!(event.getEntity() instanceof Player p)) {
            return;
        }
        Location loc = p.getLocation();
        String attacker = "";
        if (event instanceof EntityDamageByEntityEvent byEnt) {
            attacker = byEnt.getDamager().getType().name().toLowerCase(Locale.ROOT);
        }
        double hpAfter = Math.max(0.0, p.getHealth() - event.getFinalDamage());
        StringBuilder sb = baseHeader("PLAYER_DAMAGE", p.getName(), loc.getWorld());
        sb.append(",\"data\":{");
        sb.append("\"pos\":[").append(String.format(Locale.ROOT, "%.2f,%.2f,%.2f", loc.getX(), loc.getY(), loc.getZ())).append("]");
        sb.append(",\"cause\":\"").append(esc(event.getCause().name())).append("\"");
        sb.append(",\"damage\":").append(String.format(Locale.ROOT, "%.2f", event.getDamage()));
        sb.append(",\"final_damage\":").append(String.format(Locale.ROOT, "%.2f", event.getFinalDamage()));
        sb.append(",\"hp_after\":").append(String.format(Locale.ROOT, "%.1f", hpAfter));
        sb.append(",\"attacker\":\"").append(esc(attacker)).append("\"");
        sb.append("}}");
        emit(sb.toString());
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onPlayerDeath(PlayerDeathEvent event) {
        Player p = event.getEntity();
        Location loc = p.getLocation();
        StringBuilder sb = baseHeader("PLAYER_DEATH", p.getName(), loc.getWorld());
        sb.append(",\"data\":{");
        sb.append("\"pos\":[").append(String.format(Locale.ROOT, "%.2f,%.2f,%.2f", loc.getX(), loc.getY(), loc.getZ())).append("]");
        sb.append(",\"block\":[").append(loc.getBlockX()).append(',').append(loc.getBlockY()).append(',').append(loc.getBlockZ()).append(']');
        sb.append(",\"message\":\"").append(esc(String.valueOf(event.getDeathMessage()))).append("\"");
        sb.append("}}");
        emit(sb.toString());
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onPlayerRespawn(PlayerRespawnEvent event) {
        Player p = event.getPlayer();
        Location loc = event.getRespawnLocation();
        StringBuilder sb = baseHeader("PLAYER_RESPAWN", p.getName(), loc.getWorld());
        sb.append(",\"data\":{");
        sb.append("\"pos\":[").append(String.format(Locale.ROOT, "%.2f,%.2f,%.2f", loc.getX(), loc.getY(), loc.getZ())).append("]");
        sb.append(",\"bed_spawn\":").append(event.isBedSpawn());
        sb.append("}}");
        emit(sb.toString());
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onAdvancement(PlayerAdvancementDoneEvent event) {
        String key = event.getAdvancement().getKey().toString();
        if (key.contains(":recipes/")) {
            return;
        }
        Player p = event.getPlayer();
        Location loc = p.getLocation();
        StringBuilder sb = baseHeader("ADVANCEMENT", p.getName(), loc.getWorld());
        sb.append(",\"data\":{");
        sb.append("\"key\":\"").append(esc(key)).append("\"");
        sb.append(",\"pos\":[").append(String.format(Locale.ROOT, "%.2f,%.2f,%.2f", loc.getX(), loc.getY(), loc.getZ())).append("]");
        sb.append("}}");
        emit(sb.toString());
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onInventoryOpen(InventoryOpenEvent event) {
        if (!(event.getPlayer() instanceof Player p)) {
            return;
        }
        InventoryType t = event.getInventory().getType();
        if (t != InventoryType.FURNACE && t != InventoryType.BLAST_FURNACE && t != InventoryType.SMOKER
            && t != InventoryType.CHEST && t != InventoryType.BARREL && t != InventoryType.WORKBENCH) {
            return;
        }
        Location loc = event.getInventory().getLocation();
        Location ref = loc != null ? loc : p.getLocation();
        StringBuilder sb = baseHeader("INVENTORY_OPEN", p.getName(), ref.getWorld());
        sb.append(",\"data\":{");
        sb.append("\"inv_type\":\"").append(esc(t.name())).append("\"");
        sb.append(",\"pos\":[").append(ref.getBlockX()).append(',').append(ref.getBlockY()).append(',').append(ref.getBlockZ()).append(']');
        sb.append("}}");
        emit(sb.toString());
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onPlayerQuit(PlayerQuitEvent event) {
        Player p = event.getPlayer();
        StringBuilder sb = baseHeader("PLAYER_QUIT", p.getName(), p.getWorld());
        sb.append(",\"data\":{\"reason\":\"").append(esc(String.valueOf(event.getQuitMessage()))).append("\"}}");
        emit(sb.toString());
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onPlayerKick(PlayerKickEvent event) {
        Player p = event.getPlayer();
        StringBuilder sb = baseHeader("PLAYER_KICK", p.getName(), p.getWorld());
        sb.append(",\"data\":{\"reason\":\"").append(esc(String.valueOf(event.getReason()))).append("\"}}");
        emit(sb.toString());
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!"bsdebug".equalsIgnoreCase(command.getName())) {
            return false;
        }
        if (args.length == 0 || "status".equalsIgnoreCase(args[0])) {
            int tracked = 0;
            for (Player p : Bukkit.getOnlinePlayers()) {
                if (p.getGameMode() != GameMode.SPECTATOR) {
                    tracked++;
                }
            }
            sender.sendMessage(ChatColor.AQUA + "[BotSleepDebug] run=" + activeRunId
                + " events=" + totalEvents.get()
                + " dropped=" + droppedEvents.get()
                + " tracked_players=" + tracked
                + " path=" + activeLogPath);
            hotStuck.entrySet().stream()
                .sorted(Comparator.comparing(Map.Entry::getKey))
                .forEach(entry -> {
                    entry.getValue().entrySet().stream()
                        .sorted(Map.Entry.<String, Integer>comparingByValue().reversed())
                        .limit(2)
                        .forEach(cell -> sender.sendMessage(
                            ChatColor.YELLOW + "  hot: " + entry.getKey() + " @ " + cell.getKey() + " (" + cell.getValue() + " samples)"
                        ));
                });
            return true;
        }
        if ("run".equalsIgnoreCase(args[0]) && args.length >= 2) {
            rotateRun(args[1]);
            sender.sendMessage(ChatColor.GREEN + "[BotSleepDebug] rotated log to " + activeLogPath);
            return true;
        }
        if ("tp".equalsIgnoreCase(args[0]) && args.length >= 2) {
            if (!(sender instanceof Player caller)) {
                sender.sendMessage("Only players can use /bsdebug tp");
                return true;
            }
            Player target = Bukkit.getPlayerExact(args[1]);
            if (target == null) {
                sender.sendMessage(ChatColor.RED + "Player not found: " + args[1]);
                return true;
            }
            caller.teleport(target.getLocation());
            sender.sendMessage(ChatColor.GREEN + "Teleported to " + target.getName());
            return true;
        }
        if ("mark".equalsIgnoreCase(args[0]) && args.length >= 2) {
            String target = args[1];
            boolean enable;
            if (args.length >= 3) {
                enable = "on".equalsIgnoreCase(args[2]) || "true".equalsIgnoreCase(args[2]);
            } else {
                enable = !markedPlayers.contains(target);
            }
            if (enable) {
                markedPlayers.add(target);
            } else {
                markedPlayers.remove(target);
            }
            sender.sendMessage(ChatColor.AQUA + "[BotSleepDebug] particle mark for " + target + ": " + (enable ? "ON" : "OFF"));
            return true;
        }
        sender.sendMessage("Usage: /bsdebug <run <id>|status|tp <player>|mark <player> [on|off]>");
        return true;
    }
}
