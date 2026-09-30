# Tell people who log in about package reinstalls an update left waiting for
# the network, and how to finish them. Installed by calculinux-update; only
# file tests here, so logins stay fast.
case $- in
    *i*) ;;
    *) return 0 2>/dev/null || exit 0 ;;
esac

_cup_queue=/var/lib/calculinux-update/update-state.pending-reinstalls
if [ -s "$_cup_queue" ]; then
    _cup_count=$(grep -c . "$_cup_queue")
    printf '\n%s\n%s\n%s\n%s\n\n' \
        "Calculinux update: $_cup_count package(s) still need to be reinstalled" \
        "for the new system (the network was not available after the update)." \
        "  Connect to the network (uwific), then run:  cup reconcile" \
        "  List them with:  cup status"
fi
unset _cup_queue _cup_count
