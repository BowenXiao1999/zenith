use std::{io, net::ToSocketAddrs};

use socket2::{Domain, Protocol, SockAddr, Type};

pub use socket2::Socket;

/// Bind a [`TcpListener`] to addr with `SO_REUSEADDR` set to true.
pub fn bind<A: ToSocketAddrs>(addr: A) -> io::Result<Socket> {
    let socket = Socket::new(Domain::IPV4, Type::STREAM, Some(Protocol::TCP))?;
    socket.set_reuse_address(true)?;

    let address =
        SockAddr::from(addr.to_socket_addrs()?.next().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidInput, "couldn't resolve address")
        })?);
    socket.bind(&address)?;
    socket.listen(1)?;

    Ok(socket)
}
